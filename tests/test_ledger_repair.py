# -*- coding: utf-8 -*-
"""The ledger steps, each on the shape it was written for.

Every scenario is one that happened on db_dev in 2026, rebuilt the way
it was produced: a posted line deleted under the old hr_rcp (done here
in SQL - the ORM refuses it now), an employee half deleted by hand, a
reversal stamped with the day of the click.

The steps select on the whole ledger, so a test database with real data
gives them more to do than the fixture. Every assertion is therefore
about the fixture's own rows, never about totals.
"""

from datetime import date

from odoo.tests import tagged

from odoo.addons.hr_rcp.tests import test_posting_integrity as base


@tagged('post_install', '-at_install')
class TestLedgerRepair(base.RcpPostingFixture):

    def setUp(self):
        super().setUp()
        self.wizard = self.env['mj.rcp.repair'].create({})
        self.log = []
        self.AAL = self.env['account.analytic.line'].sudo()

    def _post(self, quantity=8.0):
        line = self._line(quantity)
        _state, wage = self.rcp._post_single_rcp_line_to_analytic(line)
        return line, wage

    def _delete_the_old_way(self, line):
        """What hr_rcp before 17.0.0.0.12 let anyone do."""
        self.env.flush_all()
        self.env.cr.execute(
            "DELETE FROM hr_rcp_lines_analytic_rel WHERE rcp_line_id = %s", (line.id,))
        self.env.cr.execute("DELETE FROM hr_rcp_lines WHERE id = %s", (line.id,))
        self.env.invalidate_all()

    def _rows_of(self, key_prefix):
        rows = self.AAL.search([('rcp_move_key', '=like', key_prefix + '%')])
        return rows | rows.mapped('transfer_pair_id')

    def _employee_total(self):
        self.env.flush_all()
        return sum(self.AAL.search(
            [('account_id', '=', self.e_account.id)]).mapped('amount'))

    # ------------------------------------------------------------------
    def test_a_duplicate_of_a_deleted_line_is_reversed_with_its_overheads(self):
        first, _wage = self._post()
        self._delete_the_old_way(first)
        self._post()                                   # the same hours, again
        self.assertEqual(self._employee_total(), 1600.0, "booked twice")

        self.wizard._step_orphans(self.log.append)

        self.assertEqual(self._employee_total(), 800.0, "booked once")
        orphan_rows = self._rows_of('RCP:%s:' % first.id)
        self.assertAlmostEqual(sum(orphan_rows.mapped('amount')), 0.0, places=2,
                               msg="wage and overheads cancelled on every account")
        reversals = orphan_rows.filtered(lambda r: r.rcp_operation_type == 'reverse')
        self.assertTrue(reversals.filtered('rcp_cmp_group'), "overheads too")
        for reversal in reversals:
            self.assertEqual(reversal.date, reversal.rcp_reverse_of_id.date)

    def test_running_it_twice_changes_nothing(self):
        first, _wage = self._post()
        self._delete_the_old_way(first)
        self._post()
        self.wizard._step_orphans(self.log.append)
        count = self.AAL.search_count([])
        self.wizard._step_orphans(self.log.append)
        self.assertEqual(self.AAL.search_count([]), count)

    def test_an_orphan_with_no_twin_is_left_alone(self):
        """It may be the only record of those hours."""
        first, _wage = self._post()
        self._delete_the_old_way(first)

        self.wizard._step_orphans(self.log.append)

        self.assertEqual(self._employee_total(), 800.0)
        self.assertFalse(self.AAL.search([
            ('rcp_reverse_of_id', 'in', self._rows_of('RCP:%s:' % first.id).ids)]))

    def test_a_deleted_employee_half_is_given_back(self):
        _line, wage = self._post()
        employee_half = wage.transfer_pair_id
        self.env.flush_all()
        # What the transfer layer did when the half was deleted by hand.
        self.env.cr.execute(
            "UPDATE account_analytic_line SET transfer_pair_id = NULL, "
            "one_sided_operation = true, posting_origin = NULL WHERE id = %s",
            (wage.id,))
        self.env.cr.execute("DELETE FROM account_analytic_line WHERE id = %s",
                            (employee_half.id,))
        self.env.invalidate_all()
        self.assertEqual(self._employee_total(), 0.0)

        self.wizard._step_pairs(self.log.append)

        self.assertEqual(self._employee_total(), 800.0)
        self.assertTrue(wage.transfer_pair_id)
        self.assertEqual(wage.transfer_pair_id.posting_origin, 'labor_absorption')

    def test_a_restored_half_makes_an_orphan_recognisable(self):
        """Zeinab: the live line had lost its employee half, so the
        orphan had no twin until the half came back."""
        first, _wage = self._post()
        self._delete_the_old_way(first)
        _second, wage = self._post()
        employee_half = wage.transfer_pair_id
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE account_analytic_line SET transfer_pair_id = NULL, "
            "one_sided_operation = true, posting_origin = NULL WHERE id = %s",
            (wage.id,))
        self.env.cr.execute("DELETE FROM account_analytic_line WHERE id = %s",
                            (employee_half.id,))
        self.env.invalidate_all()
        self.assertEqual(self._employee_total(), 800.0,
                         "the orphan's wage only - the live one lost its half")

        self.wizard._run_ledger(self.log.append)

        self.assertEqual(self._employee_total(), 800.0)
        self.assertTrue(wage.transfer_pair_id)

    def test_a_reversal_is_moved_to_the_month_it_cancels(self):
        line, wage = self._post()
        self.rcp._reverse_single_rcp_line(line)
        reversal = line.analytic_reversal_line_id
        stamped = reversal | reversal.transfer_pair_id | line.analytic_cmp_reversal_ids
        stamped |= line.analytic_cmp_reversal_ids.mapped('transfer_pair_id')
        self.env.flush_all()
        self.env.cr.execute("UPDATE account_analytic_line SET date = %s WHERE id = ANY(%s)",
                            (date(2030, 7, 1), stamped.ids))
        self.env.invalidate_all()

        self.wizard._step_redate(self.log.append)

        for row in stamped:
            self.assertEqual(row.date, wage.date)

    def test_a_line_in_error_is_posted_again(self):
        line = self._line(8.0)
        line.write({'status': 'error', 'analytic_last_error': 'old failure'})

        self.wizard._step_retry(self.log.append)

        self.assertEqual(line.status, 'posted')
        self.assertEqual(self._employee_total(), 800.0)

    def test_cost_centre_staff_are_excluded_and_reversed(self):
        """A012 PMs: the flag first, so RCP cannot take the hours again;
        production stays - cmp_costs counts it in the overhead divisor."""
        self.e_account.code = 'A012-E-TR001-TRCPWORKER'
        self.employee.production = True
        line, _wage = self._post()
        self.wizard._step_unbook_cost_centre_staff(self.log.append)
        self.assertTrue(self.employee.rcp_excluded)
        self.assertTrue(self.employee.production)
        self.assertEqual(line.status, 'reversed')
        self.assertEqual(self._employee_total(), 0.0)
