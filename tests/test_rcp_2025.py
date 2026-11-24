# -*- coding: utf-8 -*-
"""Cost model data the 2025 posting puts right before it posts."""

from datetime import date

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install')
class TestRcp2025CostModel(TransactionCase):

    def setUp(self):
        super().setUp()
        self.wizard = self.env['mj.rcp.repair'].create({})
        self.log = []
        Account = self.env['account.analytic.account']
        plan = self.env['account.analytic.plan'].create({'name': 'Test 2025 plan'})
        self.person = Account.create({'name': 'Ofertowanie - Zenon Test',
                                      'code': 'A938-E-ZZ911-ZTEST', 'plan_id': plan.id})
        self.employee = self.env['hr.employee'].create(
            {'name': 'Zenon Test 2025', 'account_id': self.person.id})

    def _model(self, month):
        model = self.env['cmp_personnel_costs.model'].search(
            [('date_month', '=', month), ('company_id', '=', self.env.company.id)], limit=1)
        if not model:
            self.skipTest('no personnel cost model for %s' % month)
        return model

    def _row(self, month, base):
        return self.env['cmp_personnel_costs.line'].create({
            'personnel_id': self._model(month).id, 'employee': self.employee.id,
            'basis_rem': base, 'factor': 1.24, 'performance': 0.8,
            'company_id': self.env.company.id})

    def test_the_row_a_later_month_confirms_is_kept(self):
        """Zagórny, February 2025: January's 7 965.71 next to 8 788.46,
        the base of every month after it."""
        stale, right = self._row(date(2025, 10, 1), 7965.71), self._row(date(2025, 10, 1), 8788.46)
        self._row(date(2025, 11, 1), 8788.46)
        self.wizard._duplicate_cost_rows_2025(self.log.append)
        self.assertFalse(stale.exists())
        self.assertTrue(right.exists())

    def test_no_confirmation_leaves_both_and_says_so(self):
        rows = self._row(date(2025, 12, 1), 7965.71) | self._row(date(2025, 12, 1), 8788.46)
        self.wizard._duplicate_cost_rows_2025(self.log.append)
        self.assertEqual(len(rows.exists()), 2)
        self.assertTrue(any('UNRESOLVED' in line for line in self.log))

    def test_offer_overhead_moves_off_a_person_account(self):
        cost_model = self.env['cmp_costs.model'].search([], limit=1)
        centre = self.env['account.analytic.account'].search([('code', '=', 'A038')], limit=1)
        if not cost_model or not centre or 'cmp_costs_b1.lines' not in self.env:
            self.skipTest('no cost model or A038 account')
        line = self.env['cmp_costs_b1.lines'].create({
            'cost_id': cost_model.id, 'acc_num': self.person.id,
            'description': 'godziny pracy przy ofertach'})
        other = self.env['cmp_costs_b1.lines'].create({
            'cost_id': cost_model.id, 'acc_num': self.person.id, 'description': 'samochód'})
        found_centre, found = self.wizard._offer_overhead_on_person_accounts()
        self.assertEqual(found_centre, centre)
        moved = self.env['cmp_costs_b1.lines'].browse()
        for _model, lines in found:
            moved |= lines if lines._name == 'cmp_costs_b1.lines' else moved.browse()
        self.assertIn(line, moved)
        self.assertNotIn(other, moved)
