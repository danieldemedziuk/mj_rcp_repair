# -*- coding: utf-8 -*-
"""A sales person's E-account and offer project account folded into their
A038-E-... account (decision of 06.10.2026)."""

from datetime import date
from unittest.mock import patch

from odoo.tests import tagged
from odoo.tests.common import TransactionCase

from odoo.addons.mj_rcp_repair.models import mj_rcp_repair as repair


@tagged('post_install', '-at_install')
class TestMergePersonAccounts(TransactionCase):

    def setUp(self):
        super().setUp()
        Account = self.env['account.analytic.account']
        plan = self.env['account.analytic.plan'].create({'name': 'Test merge plan'})
        self.e_account = Account.create({'name': 'Zenon Test', 'code': 'E-ZZ901-ZTEST',
                                         'plan_id': plan.id})
        self.function = Account.create({'name': 'Ofertowanie - Zenon Test',
                                        'code': 'A038-E-ZZ901-ZTEST', 'plan_id': plan.id})
        self.project_account = Account.create({'name': 'Ofertowanie - Zenon Test',
                                               'code': '39-0901', 'plan_id': plan.id})
        self.employee = self.env['hr.employee'].create(
            {'name': 'Zenon Test', 'unique_identifier': 'ZZ901'})
        self.employee.account_id = self.e_account
        self.project = self.env['project.project'].create(
            {'name': 'Ofertowanie - Zenon Test', 'analytic_account_id': self.project_account.id})
        self.project.code = '39-0901'
        AAL = self.env['account.analytic.line']
        self.pay = AAL.create({'name': 'pay', 'date': date(2026, 3, 31), 'amount': -1000.0,
                               'account_id': self.e_account.id})
        self.hours = AAL.create({'name': 'offer', 'date': date(2026, 3, 4), 'unit_amount': 8.0,
                                 'employee_id': self.employee.id, 'project_id': self.project.id,
                                 'account_id': self.project_account.id})
        self.wizard = self.env['mj.rcp.repair'].create({})
        self.log = []

    def _merge(self):
        with patch.dict(repair.OFFER_PROJECTS, {'39-0901': 'ZZ901'}):
            self.wizard._step_merge_person_accounts(self.log.append)
        self.env.invalidate_all()

    def test_everything_moves_to_the_function_account(self):
        self._merge()
        self.assertEqual(self.pay.account_id, self.function)
        self.assertEqual(self.hours.account_id, self.function)
        self.assertEqual(self.employee.account_id, self.function)
        self.assertEqual(self.project.analytic_account_id, self.function)

    def test_the_merged_accounts_are_deleted(self):
        e_id, p_id = self.e_account.id, self.project_account.id
        self._merge()
        self.assertFalse(self.env['account.analytic.account'].browse([e_id, p_id]).exists())

    def test_payroll_finds_the_merged_account(self):
        self._merge()
        self.assertIn(self.function.id,
                      self.env['sync.employee.cost']._own_employee_account_ids())

    def test_running_it_twice_changes_nothing(self):
        self._merge()
        self.log.clear()
        self._merge()
        self.assertEqual(self.employee.account_id, self.function)
        self.assertFalse([entry for entry in self.log if 'deleted ' in entry])
