# -*- coding: utf-8 -*-
"""Clearing an RCP period: a wage left behind by a deleted line."""

from datetime import date

from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install')
class TestCleanPeriod(TransactionCase):

    def setUp(self):
        super().setUp()
        Account = self.env['account.analytic.account']
        plan = self.env['account.analytic.plan'].create({'name': 'Test clean plan'})
        self.e_account = Account.create({'name': 'E', 'code': 'E-ZZ921-ZCLEAN', 'plan_id': plan.id})
        self.project = Account.create({'name': 'P', 'code': '39-0921', 'plan_id': plan.id})
        self.posted = self.env['account.analytic.line'].sudo().with_context(
            allow_transfer_document_posting=True).create({
                'name': 'wage', 'date': date(2030, 6, 21), 'amount': -500.0,
                'account_id': self.e_account.id, 'destination_account_id': self.project.id,
                'rcp_operation_type': 'post', 'rcp_move_key': 'RCP:999999901:POST',
                'posting_origin': 'labor_absorption'})
        self.wizard = self.env['mj.rcp.repair'].create({
            'clean_date_from': date(2030, 6, 1), 'clean_date_to': date(2030, 6, 30)})

    def test_orphan_wage_is_found_and_netted(self):
        orphans = self.wizard._orphan_wage_postings(date(2030, 6, 1), date(2030, 6, 30))
        self.assertEqual(orphans, self.posted)
        storno = self.wizard._reverse_orphan_wage(self.posted)
        rows = self.posted | self.posted.transfer_pair_id | storno | storno.transfer_pair_id
        for account in (self.e_account, self.project):
            self.assertAlmostEqual(
                sum(rows.filtered(lambda r: r.account_id == account).mapped('amount')), 0.0)
        self.assertFalse(self.wizard._orphan_wage_postings(date(2030, 6, 1), date(2030, 6, 30)),
                         "a second run finds nothing left to reverse")

    def test_the_whole_step_runs_and_reports(self):
        log = []
        self.wizard._step_clean_period(log.append)
        self.assertTrue(any('orphan wage(s) reversed' in line for line in log))
        self.assertTrue(any(line == 'accounts not netting to zero: none' for line in log), log)
