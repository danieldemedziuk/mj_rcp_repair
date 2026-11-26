# -*- coding: utf-8 -*-
"""The efficiency data repair, step by step, in a company of its own so no
real RCP week on the database is ever in scope."""

from datetime import date, datetime

from odoo.fields import Command
from odoo.tests import TransactionCase, tagged

# A day the task scheduler knows: daily_task refuses a leave on any other.
MONDAY = date(2026, 3, 2)


@tagged('post_install', '-at_install')
class TestEfficiencyRepair(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env['res.company'].create({
            'name': 'Efficiency Repair Test Co', 'sheet_range': 'WEEKLY'})
        cls.env.user.company_ids |= cls.company
        env = cls.env(context=dict(cls.env.context, allowed_company_ids=[cls.company.id]))
        cls.cenv = env
        cls.user = env['res.users'].with_context(no_reset_password=True).create({
            'name': 'Repair Worker', 'login': 'eff_repair_worker',
            'company_id': cls.company.id,
            'company_ids': [Command.set([cls.company.id])],
            'groups_id': [Command.set([
                env.ref('base.group_user').id,
                env.ref('hr_timesheet.group_hr_timesheet_user').id])],
        })
        cls.employee = env['hr.employee'].create({
            'name': 'Repair Worker', 'company_id': cls.company.id,
            'user_id': cls.user.id,
        })
        cls.project = env['project.project'].create({
            'name': 'Repair Customer Project', 'company_id': cls.company.id,
            'allow_timesheets': True,
        })
        cls.internal = cls.company.internal_project_id
        cls.leave_type = env['hr.leave.type'].create({
            'name': 'Repair Test Leave', 'requires_allocation': 'no',
            'leave_validation_type': 'no_validation', 'timesheet_generate': True,
            'company_id': False,
        })
        cls.admin = env.ref('base.user_admin')
        cls.admin.company_ids |= cls.company

    def _wizard(self):
        return self.env['mj.efficiency.repair'].with_user(self.admin).with_company(
            self.company).create({
                'date_from': date(2026, 1, 1), 'date_to': date(2026, 12, 31),
                'company_id': self.company.id})

    def _leave(self, day):
        return self.cenv['hr.leave'].create({
            'employee_id': self.employee.id,
            'holiday_status_id': self.leave_type.id,
            'request_date_from': day, 'request_date_to': day,
            'date_from': datetime.combine(day, datetime.min.time()).replace(hour=7),
            'date_to': datetime.combine(day, datetime.min.time()).replace(hour=15),
        })

    def _lines(self, **extra):
        domain = [('employee_id', '=', self.employee.id)]
        domain += [(k, '=', v) for k, v in extra.items()]
        return self.env['account.analytic.line'].sudo().search(domain)

    # -- 2 ---------------------------------------------------------------
    def test_manual_absence_on_a_leave_day_is_removed(self):
        leave = self._leave(MONDAY)
        self.assertTrue(leave.timesheet_ids, "precondition: the leave made its line")
        manual = self._manual_absence(8.0)
        self._wizard().action_repair()
        self.assertFalse(manual.exists())
        self.assertTrue(leave.timesheet_ids.exists(), "the leave's own line stays")

    def test_a_leave_cancelled_by_hand_is_left_for_hr(self):
        """A negative manual line takes the leave back: the person worked.
        Removing it would put the absence back on the books."""
        self._leave(MONDAY)
        negative = self._manual_absence(-8.0)
        wizard = self._wizard()
        wizard.action_repair()
        self.assertTrue(negative.exists())
        self.assertIn('Repair Worker', wizard.output)

    def _manual_absence(self, hours):
        task = self.env['project.task'].sudo().create({
            'name': 'Dni wolne', 'project_id': self.internal.id,
            'mj_absence_class': 'entitled',
        })
        return self.env['account.analytic.line'].sudo().create({
            'name': 'Dni wolne', 'date': MONDAY, 'unit_amount': hours,
            'employee_id': self.employee.id, 'project_id': self.internal.id,
            'task_id': task.id, 'company_id': self.company.id,
        })

    # -- 3 ---------------------------------------------------------------
    def test_a_leave_without_lines_gets_them_once(self):
        leave = self._leave(MONDAY)
        lines = leave.timesheet_ids
        lines.write({'holiday_id': False})
        lines.unlink()
        self.assertFalse(leave.timesheet_ids)

        self._wizard().action_repair()
        self.assertEqual(sum(leave.timesheet_ids.mapped('unit_amount')), 8.0)

        self._wizard().action_repair()
        self.assertEqual(len(leave.timesheet_ids), 1, "a second run adds nothing")

    def test_a_cancelled_leave_gets_no_lines(self):
        leave = self._leave(MONDAY)
        # The shape a cancellation leaves behind: archived, lines removed,
        # state still 'validate'. Set directly - Odoo refuses to cancel a
        # past leave from code, and what is tested is the repair.
        lines = leave.timesheet_ids
        lines.write({'holiday_id': False})
        lines.unlink()
        self.env.cr.execute("UPDATE hr_leave SET active = FALSE WHERE id = %s", (leave.id,))
        leave.invalidate_recordset(['active'])
        self.assertEqual(leave.state, 'validate')
        self._wizard().action_repair()
        self.assertFalse(self._lines(holiday_id=leave.id))

    def test_preview_keeps_nothing(self):
        leave = self._leave(MONDAY)
        lines = leave.timesheet_ids
        lines.write({'holiday_id': False})
        lines.unlink()
        wizard = self._wizard()
        wizard.action_preview()
        self.assertFalse(leave.timesheet_ids)
        self.assertIn('PREVIEW', wizard.output)

    # -- 4 ---------------------------------------------------------------
    def test_a_detached_line_goes_back_to_its_approved_sheet(self):
        sheet = self.cenv['hr_timesheet.sheet'].create({
            'employee_id': self.employee.id, 'company_id': self.company.id,
            'date_start': MONDAY, 'date_end': date(2026, 3, 8),
        })
        line = self.cenv['account.analytic.line'].create({
            'name': 'work', 'date': MONDAY, 'unit_amount': 8.0,
            'employee_id': self.employee.id, 'project_id': self.project.id,
            'company_id': self.company.id,
        })
        self.assertEqual(line.sheet_id, sheet)
        sheet.state = 'done'
        self.env.cr.execute(
            "UPDATE account_analytic_line SET sheet_id = NULL WHERE id = %s", (line.id,))
        line.invalidate_recordset(['sheet_id'])

        self._wizard().action_repair()
        line.invalidate_recordset(['sheet_id'])
        self.assertEqual(line.sheet_id, sheet)

    # -- 1 ---------------------------------------------------------------
    def test_a_correction_of_a_posting_rcp_reversed_is_removed(self):
        Line = self.env['account.analytic.line'].sudo().with_context(
            skip_transfer_pair_creation=True)
        plan = self.env['account.analytic.plan'].create({'name': 'Repair Plan'})
        Account = self.env['account.analytic.account']
        e_acc = Account.create({'name': 'E', 'code': 'E-RT001-RTEST',
                                'plan_id': plan.id, 'company_id': self.company.id})
        p_acc = Account.create({'name': 'P', 'code': '31-0001',
                                'plan_id': plan.id, 'company_id': self.company.id})

        def pair(charged, funded, amount, **vals):
            a, b = Line.create([
                dict(vals, account_id=charged.id, amount=-amount, date=MONDAY,
                     company_id=self.company.id, name=vals.get('name', 'x')),
                dict(vals, account_id=funded.id, amount=amount, date=MONDAY,
                     company_id=self.company.id, name=vals.get('name', 'x')),
            ])
            a.transfer_pair_id, b.transfer_pair_id = b, a
            return a, b

        original, _o2 = pair(e_acc, p_acc, 100.0, rcp_move_key='RCP:999001:POST')
        storno, _s2 = pair(p_acc, e_acc, 100.0, rcp_reverse_of_id=original.id,
                           rcp_move_key='RCP:999001:REVERSE:%s' % original.id)
        (original | _o2 | storno | _s2).write({'is_reversed': True})
        correction, correction_pair = pair(
            p_acc, e_acc, 100.0, rcp_reverse_of_id=original.id,
            name='Korekta: wycofanie absorpcji robocizny (linia %s)' % original.id)

        self._wizard().action_repair()
        self.assertFalse(correction.exists())
        self.assertFalse(correction_pair.exists())
        self.assertTrue(storno.exists())
