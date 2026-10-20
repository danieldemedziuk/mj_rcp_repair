# -*- coding: utf-8 -*-
"""Unposted copies of a sheet's lines, as the operation of 05.10.2026
left them: one holding the timesheets of a posted line, one repeating a
posted line hour for hour with no timesheets at all.
"""

from datetime import date

from odoo.tests import tagged

from odoo.addons.hr_rcp.tests import test_posting_integrity as base


@tagged('post_install', '-at_install')
class TestDuplicateLines(base.RcpPostingFixture):

    def setUp(self):
        super().setUp()
        self.wizard = self.env['mj.rcp.repair'].create({})
        self.log = []

    def _timesheet(self, hours=8.0):
        return self.env['account.analytic.line'].create({
            'name': 'work', 'date': date(2030, 6, 4), 'unit_amount': hours,
            'employee_id': self.employee.id, 'project_id': self.project.id,
            'account_id': self.project_account.id,
        })

    def _posted(self, timesheets):
        line = self._line(8.0)
        line.analytic_ids = [(6, 0, timesheets.ids)]
        self.rcp._post_single_rcp_line_to_analytic(line)
        return line

    def test_a_copy_holding_the_same_timesheets_is_deleted(self):
        timesheet = self._timesheet()
        original = self._posted(timesheet)
        copy = self._line(8.0)
        copy.analytic_ids = [(6, 0, timesheet.ids)]

        self.wizard._step_duplicate_lines(self.log.append)

        self.assertFalse(copy.exists())
        self.assertTrue(original.exists())
        self.assertIn(timesheet, original.analytic_ids)
        self.assertTrue(timesheet.rcp_checked, "the original still takes it")

    def test_a_copy_without_timesheets_of_a_posted_line_is_deleted(self):
        self._posted(self._timesheet())
        copy = self._line(8.0)

        self.wizard._step_duplicate_lines(self.log.append)

        self.assertFalse(copy.exists())

    def test_a_line_with_its_own_timesheets_is_kept(self):
        self._posted(self._timesheet())
        own = self._line(4.0)
        own.analytic_ids = [(6, 0, self._timesheet(4.0).ids)]

        self.wizard._step_duplicate_lines(self.log.append)

        self.assertTrue(own.exists())

    def test_a_line_without_timesheets_and_other_hours_is_kept(self):
        """Hours typed in by hand - nothing says they are a copy."""
        self._posted(self._timesheet())
        manual = self._line(3.0)

        self.wizard._step_duplicate_lines(self.log.append)

        self.assertTrue(manual.exists())

    def test_running_it_twice_changes_nothing(self):
        timesheet = self._timesheet()
        self._posted(timesheet)
        copy = self._line(8.0)
        copy.analytic_ids = [(6, 0, timesheet.ids)]
        self.wizard._step_duplicate_lines(self.log.append)
        lines = self.rcp.rcp_lines
        self.wizard._step_duplicate_lines(self.log.append)
        self.assertEqual(self.rcp.rcp_lines, lines)
