# -*- coding: utf-8 -*-
"""The repair, driven from the interface instead of a shell.

Every step here was a standalone script first. On a Docker deployment
there is no shell to pipe one into, so the logic moved into a model and
the scripts became a thin caller. Keeping one copy matters more than
keeping the scripts: the two drifted once already, and the divergence
(mirrors that recorded what they cancelled only in a description) made
the audit permanently wrong while the repair itself worked.

Transactions
------------
A wizard has no cursor of its own to commit, and it does not need one.
Everything runs inside a savepoint: on a dry run the savepoint is rolled
back and the log is all that survives, on a real run it is released and
the request commits it. `simulate` rolls back either way - recomputing a
week is how you find out what a recompute would do, not something to
keep.
"""

import re
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Below this, sheet and ledger are treated as agreeing: `a_cost` and
# `spec_cost` are each rounded to two decimals per hour, so a line's
# total drifts by grosze from the rates it is posted from.
RATE_TOLERANCE = 0.01

# A sheet's result is the sum of per-line roundings of the overhead
# rates; on the production copy the worst week came to 10.88 zł.
SHEET_RESULT_TOLERANCE = 50.0

MIRROR_MARKER ='Korekta: wycofanie absorpcji robocizny'

REQUIRED_VERSIONS = {
    # 17.0.0.0.12: a posted line and its ledger rows cannot be deleted
    # and a reversal is dated on its original. Without both the ledger
    # steps below are undone by the next recompute. 17.0.0.0.16: the
    # cost centre account standard and the person-account guards the
    # analytics phase relies on.
    # 17.0.0.0.17: hours lowered or moved after posting are corrected.
    # 17.0.0.0.18: sheet totals read from the ledger.
    # 17.0.0.0.19: a merged A038-E-... account is the employee's own.
    # 17.0.0.0.22: changing a code or archiving warns instead of refusing.
    # 17.0.0.0.28: the CMP side (rcp_cmp_side) the duplicate stornos set.
    # 17.0.0.0.29: rcp_excluded keeps an employee's timesheets out of RCP.
    'hr_rcp': '17.0.0.0.29',
    # 17.0.1.8.1: invoices of an offer project reach its A038-E-... account.
    'project_invoice_flow': '17.0.1.8.1',
    'mj_finance_core': '17.0.1.3.1',
    # The analytics phase: payroll matched by account code only, the
    # reversal of a posted transfer document, codes given once.
    # 17.0.9.9.2: payroll reaches a merged A038-E-... account.
    'sync_hub': '17.0.9.9.2',
    'analytic_cost_control': '17.0.4.18.1',
    'hr_extension': '17.0.0.0.8',
    'project_timesheet_control': '17.0.1.6.0',
}
# Cost centres whose per-person account became the person's only account
# (decision of 06.10.2026): pay, function costs and the hours on their own
# offer project all on A038-E-<E-code>. The E-account and the offer
# project's account are merged into it and deleted.
# One account per person for pay and function: the person's E-account is
# folded into their Axxx-E-<code> account. A038 (sales, 06.10.2026) and
# A012 (project management, 14.10.2026). The first entry is where a missing
# offer-project account is created.
MERGED_COST_CENTRES = ('A038', 'A012')
# Cost centres whose people are not costed to projects through RCP: their
# hours stay on the timesheets, but every 2026 posting of them is reversed
# (decision of 14.10.2026 - project managers, A012; they are to be funded
# from the offers instead, in a later stage).
UNBOOKED_COST_CENTRES = ('A012',)
# Offer projects run for one person: project number -> employee identifier.
OFFER_PROJECTS = {
    '26-0072': 'DP002',     # Dawid Pawelec
    '26-0073': 'GR001',     # Grzegorz Rudnicki
    '26-0075': 'KB002',     # Karol Bochenek
    '26-0076': 'MT003',     # Marcin Targoński
    '26-0077': 'MJ001',     # Mateusz Jelonek
    '26-0074': 'JR001',     # Janusz Rudnicki
    '26-0084': 'MI001',     # Marek Ilnicki
    '26-0137': 'KB003',     # Konrad Bąkowski
}
# Projects that book straight on their cost centre's account: the general
# offer project (sales support books there when idle) and the year's service
# project. Their own project-numbered account is merged into the centre.
GENERAL_OFFER_PROJECTS = {
    '26-0039': 'A038',      # Ofertowanie realizacja
    '26-0021': 'A046',      # A046 Serwis 2026
    # Training: one account for the overhead that funds it, the invoices
    # and the hours spent on it (decision of 07.10.2026).
    '26-0071': 'A011',      # A011 Szkolenia zewnętrzne (was A011-1)
    '26-0033': 'A011',      # A011 Szkolenia wewnętrzne (was A011-2)
    '26-0089': 'A011',      # A011 Akademia PCS (was A011-3)
}
# Cost centres that are ONE account (decision of 08.10.2026): every
# account whose code matches a pattern is folded into the centre, whether
# or not a project still points at it - on production the training
# accounts A011-1/-2/-3 do not all hang off the projects listed above.
CENTRE_ACCOUNT_FOLDS = {
    'A011': ('A011-%',),    # Szkolenia: A011-1, A011-2, A011-3, ...
    # Serwis: the 2026 service project's account, and the archived
    # "Serwis, Team 1 II 2025" one it took over from on 19.01.2026
    # (RCP postings only - nothing synced from Enterprise; 20.10.2026).
    'A046': ('26-0021', '25-0100'),
    # A044 exists twice: "hale produkcyjne" (archived 20.04.2026) and
    # "Warsztat". One cost centre (decision of 09.10.2026): the archived one
    # is folded into the active one - its lines, the CMP rates still
    # pointing at it (B3) and the stale counterpart on 5 320 postings.
    'A044': ('A044',),
}
# Not offer projects of anybody in sales (Sabina Prężyna).
ARCHIVED_PROJECTS = ('26-0078',)
# Sales staff who left: their account and projects are archived.
LEFT_SALES_STAFF = ('KB002', 'MJ001', 'RU001', 'SZ001')
# Many2many relations of transient wizards: a row there is a selection, not
# data, and is dropped rather than repointed (it may already hold the target).
SELECTION_RELATIONS = ('sync_hub_emp_cost_account_rel',)

# The Enterprise company the sync crons import for (cron_jobs.xml).
SYNC_COMPANY_ID = 1
# Analytic cost tracking - and so both syncs - start here.
SYNC_DATE_FROM = date(2026, 1, 1)
SALARY_SYNC_KEY = 'account.move.line.salary_cost'


def _version_tuple(version):
    """'17.0.1.2.10' -> (17, 0, 1, 2, 10), so versions compare as numbers."""
    parts = []
    for chunk in (version or '').split('.'):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts)


class _DryRun(Exception):
    """Raised to unwind a savepoint once a step has reported itself."""


class MjRcpRepair(models.TransientModel):
    _name = 'mj.rcp.repair'
    _description = 'RCP Repair (one-off)'

    step = fields.Selection([
        ('preflight', '0. Preflight - blockers'),
        ('audit', '1. Audit - what is wrong and how much'),
        ('status', '2. Repair line statuses'),
        ('reverse', '3. Reverse the wage postings'),
        ('mirror', '4. Cancel rows with no RCP line'),
        ('simulate', '5. Simulate the recompute (always rolled back)'),
        ('overheads', '6. Rebuild and post the overheads'),
        ('reprice', '7. Reprice lines stuck at the productive rate'),
        ('verify', '8. Verify'),
        ('pairs', '9. Ledger: give one-sided postings their other half'),
        ('orphans', '10. Ledger: reverse duplicates of deleted lines'),
        ('redate', '11. Ledger: date reversals on their originals'),
        ('link', '12. Ledger: attach timesheets missing from posted lines'),
        ('retry', '13. Ledger: retry lines in error'),
        ('reconcile', '14. Ledger: reconcile RCP against the ledger'),
    ], string='Step', required=True, default='preflight')

    mpk = fields.Char(
        string='Limit to MPK',
        help="Analytic codes separated by commas, e.g. 26-0142. Steps 3, "
             "5, 6 and 7 work on whole RCP weeks, so they refuse to run "
             "without a limit: an unnarrowed run touches every week any "
             "non-absorbing project appears in.")
    apply_changes = fields.Boolean(
        string='Write the changes',
        help="Leave this off first and read the report. With it off "
             "nothing is kept, whatever the step does.")
    company_id = fields.Many2one(
        'res.company', string='Company', required=True,
        default=lambda self: self.env.company,
        help="hr_rcp scopes its overhead lookups by the active company, "
             "so the repair has to run once per company that has "
             "non-absorbing projects.")
    output = fields.Text(string='Report', readonly=True)
    show_advanced = fields.Boolean(
        string='Run the steps by hand',
        help="The steps in the order the automatic run uses them, one at "
             "a time. Only needed to take a single project apart, or to "
             "reprice - which the automatic run deliberately leaves "
             "alone.")

    # ------------------------------------------------------------------
    # selection - the same predicate the posting guard uses
    # ------------------------------------------------------------------
    def _unproductive_projects(self):
        """Exactly what `hr_rcp._is_unproductive_project` will not absorb.

        `unproductive` OR, where mj_finance_core is installed,
        `mj_is_idle_time`. Selecting on the idle flag alone covers
        bezrobocie and silently skips autocorrections, whose only marker
        is `unproductive` set by hand.
        """
        Project = self.env['project.project'].with_context(active_test=False)
        domain = [('unproductive', '=', True)]
        if 'mj_is_idle_time' in Project._fields:
            domain = ['|', ('unproductive', '=', True),
                      ('mj_is_idle_time', '=', True)]
        return Project.search(domain)

    def _scope_codes(self):
        codes = [c for c in self._unproductive_projects().mapped(
            'analytic_account_id.code') if c]
        # The automatic run narrows the scope itself, one project at a
        # time, instead of asking anybody to type codes in. The manual
        # field stays for picking a single project apart by hand.
        forced = self.env.context.get('mj_rcp_scope')
        if forced:
            return tuple(sorted(c for c in codes if c in set(forced)))
        wanted = [c.strip() for c in (self.mpk or '').split(',') if c.strip()]
        if wanted:
            unknown = sorted(set(wanted) - set(codes))
            if unknown:
                # Naming a project the guard does not consider
                # unproductive would rebuild it at the unproductive rate.
                raise UserError(_(
                    "These codes are not non-absorbing projects: %s",
                    ', '.join(unknown)))
            codes = [c for c in codes if c in set(wanted)]
        return tuple(sorted(codes))

    def _require_scope(self):
        if self.env.context.get('mj_rcp_scope'):
            return
        if not (self.mpk or '').strip():
            raise UserError(_(
                "This step works on whole RCP weeks. Fill in \"Limit to "
                "MPK\" with the projects you are repairing and run it "
                "once per project."))

    def _live_wage_rows(self, accounts):
        """Main wage transfers on those projects that still stand.

        Overheads (`rcp_cmp_group`) are correct on a non-absorbing
        project and stay. A row already carrying a reversal nets to zero
        as it is and must not be corrected twice.
        """
        AAL = self.env['account.analytic.line'].sudo()
        if not accounts:
            return AAL.browse()
        rows = AAL.search([
            ('rcp_cmp_group', '=', False),
            ('rcp_move_key', '!=', False),
            ('transfer_type', '=', 'charge'),
            ('rcp_operation_type', '!=', 'reverse'),
            '|',
            ('account_id', 'in', accounts.ids),
            ('destination_account_id', 'in', accounts.ids),
        ], order='date, id')
        cancelled = set(AAL.search(
            [('rcp_reverse_of_id', 'in', rows.ids)]).mapped(
                'rcp_reverse_of_id.id'))
        # A transfer from an account to itself moved nothing: a sales
        # person's hours on their own offer project, once project and
        # person share one account (A038-E-...).
        return rows.filtered(lambda row: row.id not in cancelled
                             and row.account_id != row.destination_account_id)

    @api.model
    def _employee_effect(self, row):
        """(employee account, project account, what the E account holds).

        Which side the E account is on carries the direction: a normal
        (wrongly productive) posting charges the project, an inverted one
        charges the employee account. Reading `amount` alone gets the
        sign wrong on those.
        """
        # The employee's side: their E-account, or the account they own
        # (A038-E-...) - not a merged account standing as somebody else's
        # project.
        on_employee = ((row.account_id.code or '').startswith('E-')
                       or row.account_id == row.employee_id.account_id)
        e_side = row.account_id if on_employee else row.destination_account_id
        p_side = row.destination_account_id if on_employee else row.account_id
        return e_side, p_side, (row.amount if on_employee else abs(row.amount))

    def _person_account_ids(self):
        """Accounts a person's pay sits on: every E-account, and the
        function accounts that replaced one (A038-E-...)."""
        Account = self.env['account.analytic.account'].sudo().with_context(active_test=False)
        return set(Account.search([('code', '=like', 'E-%')]).ids) | set(
            self.env['sync.employee.cost']._own_employee_account_ids())

    def _mispriced_pending(self, log=None):
        """Pending lines whose stated rate is not the one that will book.

        The expected rate is per employee and per month, so it is asked
        of the same helper `_assert_unit_cost_matches` uses rather than
        guessed from what is already posted.
        """
        codes = self._scope_codes()
        pending = self.env['hr.rcp.lines'].sudo().search(
            [('mpk', 'in', list(codes)), ('status', 'in', ('new', 'error'))])
        wrong, undetermined, cache = [], [], {}
        for line in pending:
            if not line.quantity:
                continue          # a line with no hours is not rate-checked
            try:
                personnel = line.rcp_id._get_rcp_personnel_cost_line(
                    line.description,
                    line.accounting_date or line.rcp_id.date, cmp_cache=cache)
                expected = line.rcp_id._expected_unit_cost(personnel, True)
            except Exception as error:
                # Historic months with no personnel cost row for that
                # employee. Nothing can be said about the rate and the
                # line cannot be posted either - counted, not listed.
                undetermined.append((line, str(error).strip()))
                continue
            if abs((line.unit_cost or 0.0) - expected) > RATE_TOLERANCE:
                wrong.append((line, expected))
        if log and undetermined:
            log("%d line(s) whose expected rate cannot be determined "
                "(no personnel cost row for that employee/month)"
                % len(undetermined))
            for line, message in undetermined[:3]:
                log("   line %-7s %s  %s"
                    % (line.id, line.accounting_date, message[:70]))
        return pending, wrong

    # ------------------------------------------------------------------
    # steps
    # ------------------------------------------------------------------
    def _step_preflight(self, log):
        """Blockers. Reports, never fixes.

        Each of these has bitten the repair at least once: a project
        flagged idle but not unproductive still absorbs; a project with
        no analytic account is invisible to every figure below; a mirror
        with no link to what it cancels makes the audit permanently
        wrong.
        """
        problems = 0
        Project = self.env['project.project'].with_context(active_test=False)

        installed = {
            module.name: module.latest_version
            for module in self.env['ir.module.module'].sudo().search(
                [('name', 'in', list(REQUIRED_VERSIONS))])}
        for name, wanted in REQUIRED_VERSIONS.items():
            have = installed.get(name) or ''
            log("   %-18s installed %-12s required at least %s"
                % (name, have or '(absent)', wanted))
            # Compared as numbers, not strings: '17.0.1.2.10' sorts
            # before '17.0.1.2.2' as text, and a newer build is fine -
            # what matters is not being older than the one that carries
            # the guard.
            if _version_tuple(have) < _version_tuple(wanted):
                problems += 1
                log("   BLOCKER: %s is older than required - the guard is "
                    "not installed and the next posting would undo this "
                    "repair." % name)

        # Repaired rather than reported: `_mj_sync_unproductive` is the
        # module's own idempotent sync, and an idle project with the flag
        # off is the exact shape that put wages on six employee accounts.
        # Refusing to start over something one call fixes would be
        # pedantry, not safety.
        if 'mj_is_idle_time' in Project._fields:
            drift = Project.search([('mj_is_idle_time', '=', True),
                                    ('unproductive', '=', False)])
            if drift:
                log("%d idle project(s) had `unproductive` off - putting it "
                    "back:" % len(drift))
                for project in drift[:10]:
                    log("   id %-7s %s" % (project.id, project.display_name))
                drift._mj_sync_unproductive()

        no_account = self._unproductive_projects().filtered(
            lambda p: not p.analytic_account_id)
        if no_account:
            # A warning, not a blocker: nothing can be attributed to a
            # project with no account, so there is nothing here to
            # repair - but somebody should know they are invisible.
            log("WARNING: %d non-absorbing project(s) with no analytic "
                "account - invisible to this tool:" % len(no_account))
            for project in no_account[:10]:
                log("   id %-7s %s" % (project.id, project.display_name))

        unlinked = self.env['account.analytic.line'].sudo().search_count(
            [('name', 'like', MIRROR_MARKER),
             ('rcp_reverse_of_id', '=', False)])
        if unlinked:
            # Not a blocker: step 4 reconnects these. Saying so beats
            # stopping a run that was about to fix it.
            log("note: %d mirror line(s) have no link to what they cancel - "
                "step 4 reconnects them." % unlinked)

        log("")
        log("preflight: %s" % ("%d problem(s) above" % problems
                               if problems else "clear"))
        return problems

    def _step_audit(self, log, brief=False):
        projects = self._unproductive_projects()
        accounts = projects.mapped('analytic_account_id')
        log("non-absorbing projects: %d (with an analytic account: %d)"
            % (len(projects), len(accounts)))

        rows = self._live_wage_rows(accounts)
        log("live main wage transfers: %d" % len(rows))

        by_project, by_employee, by_month = {}, {}, {}
        no_rcp_line = self.env['account.analytic.line'].browse()
        for row in rows:
            e_side, p_side, effect = self._employee_effect(row)
            pk = p_side.code or p_side.name
            ek = e_side.code or e_side.name
            by_project[pk] = by_project.get(pk, 0.0) + effect
            by_employee[ek] = by_employee.get(ek, 0.0) + effect
            mk = row.date.strftime('%Y-%m') if row.date else '(no date)'
            by_month[mk] = by_month.get(mk, 0.0) + effect
            if not row.rcp_line_id:
                no_rcp_line |= row

        for title, data in (('by project', by_project),
                            ('by employee account', by_employee),
                            ('by month', by_month)):
            if data:
                log("")
                log("%s:" % title)
                for key in sorted(data, key=lambda k: -abs(data[k])):
                    log("   %-26s %12.2f" % (key, data[key]))

        total = sum(by_employee.values())
        log("")
        log("TOTAL on employee accounts: %.2f" % total)

        pending, refused = self._mispriced_pending(None if brief else log)
        if pending and not brief:
            log("")
            log("pending sheet lines in scope: %d (%.2f)"
                % (len(pending), sum((l.quantity or 0.0) * (l.unit_cost or 0.0)
                                     for l in pending)))
            if refused:
                log("   %d WILL BE REFUSED - stated rate is not the "
                    "unproductive rate:" % len(refused))
                for line, expected in refused[:15]:
                    log("      line %-7s %s %6.1f h  states %8.2f  "
                        "expects %8.2f"
                        % (line.id, line.accounting_date, line.quantity or 0.0,
                           line.unit_cost or 0.0, expected))
                if len(refused) > 15:
                    log("      ... and %d more" % (len(refused) - 15))
                log("   Step 7 deletes and recomputes them - merging hours "
                    "does not rewrite a rate that is already frozen.")
            else:
                log("   all pending lines carry the unproductive rate.")

        reversible = rows - no_rcp_line
        needs_status = reversible.filtered(
            lambda r: r.rcp_line_id.status == 'new')
        log("")
        log("routing:")
        log("   step 3 (has an RCP line) : %d" % len(reversible))
        log("        of which status 'new' : %d  -> step 2 first"
            % len(needs_status))
        log("   step 4 (no RCP line)     : %d (%.2f)"
            % (len(no_rcp_line),
               sum(abs(r.amount or 0.0) for r in no_rcp_line)))
        return total

    def _step_status(self, log):
        """Make hr.rcp.lines.status describe what the ledger holds.

        The 17.0.0.0.6 pre-migration rewrote every `booked` line to
        `new`, including lines whose posting already existed, and
        re-posting does not repair it: the posting sees the row, returns
        'skipped' and never touches the status. The reversal button only
        offers 'posted' lines, so the lie blocks every correction.
        """
        Lines = self.env['hr.rcp.lines'].sudo()
        candidates = Lines.search(
            [('status', '=', 'new'), ('analytic_posted_line_id', '!=', False)])
        # A line whose posting was already reversed belongs in another
        # bucket and must not be called 'posted'.
        to_fix = candidates.filtered(lambda l: not l.analytic_reversal_line_id)
        log("lines 'new' carrying a posting: %d  -> to mark posted: %d"
            % (len(candidates), len(to_fix)))
        if to_fix:
            log("ids (for the record): %s"
                % ', '.join(str(i) for i in sorted(to_fix.ids)[:50])
                + (' ...' if len(to_fix) > 50 else ''))
            # status is in the write() hook that keeps `rcp_checked` in
            # step, so this goes through the ORM rather than SQL.
            to_fix.write({'status': 'posted'})
            log("marked %d line(s) posted" % len(to_fix))

    def _documents_for(self, codes):
        return self.env['hr.rcp.lines'].sudo().search(
            [('mpk', 'in', list(codes))]).mapped('rcp_id').sorted('date')

    def _step_reverse(self, log):
        """Reverse the wage postings natively, re-dated onto the originals.

        Native rather than a hand-written mirror because
        `project_charter` reads the project's RCP cost straight off
        `hr.rcp.lines` and skips only 'reversed' lines: a mirror in the
        ledger alone would correct the ledger and leave the budget
        claiming the wage.

        Re-dated because `_reverse_single_rcp_line` used to stamp today:
        the month that carried the wage kept it and the current month
        got the credit. hr_rcp 17.0.0.0.10 dates the reversal on the
        original itself, so this step is now a no-op kept for databases
        still running an older hr_rcp.
        """
        self._require_scope()
        codes = self._scope_codes()
        Lines = self.env['hr.rcp.lines'].sudo()
        targets = Lines.search([
            ('mpk', 'in', list(codes)), ('status', '=', 'posted'),
            ('analytic_posted_line_id', '!=', False)], order='id')
        documents = targets.mapped('rcp_id')
        total = sum(abs(l.analytic_posted_line_id.amount or 0.0)
                    for l in targets)
        log("posted wage lines in scope: %d (%.2f)" % (len(targets), total))
        for line in targets[:40]:
            log("   rcp_line %-7s %s %-26s %10.2f%s"
                % (line.id, line.analytic_posted_line_id.date,
                   (line.description.name or '-')[:26],
                   line.analytic_posted_line_id.amount,
                   '  [LOCKED]' if line.rcp_id.locked else ''))
        if not targets:
            return

        # Unlock, reverse and re-lock in one go. The savepoint around the
        # whole step means a failure anywhere leaves the weeks locked as
        # they were.
        relock = documents.filtered('locked')
        if relock:
            relock.write({'locked': False})
        moved = 0
        for line in targets:
            original_date = line.analytic_posted_line_id.date
            state, _payload = line.rcp_id._reverse_single_rcp_line(line)
            if state != 'reversed':
                log("   line %s: %s" % (line.id, state))
                continue
            reversals = (line.analytic_reversal_line_id
                         | line.analytic_cmp_reversal_ids)
            for reversal in reversals:
                target_date = original_date
                if reversal.rcp_reverse_of_id:
                    target_date = reversal.rcp_reverse_of_id.date
                halves = reversal | reversal.transfer_pair_id
                halves.write({'date': target_date})
                moved += len(halves)
        if relock:
            relock.write({'locked': True})
        log("reversed %d line(s), re-dated %d analytic row(s)"
            % (len(targets), moved))

    def _step_mirror(self, log):
        """Cancel the rows no native reversal can reach.

        These carry no RCP line, so there is no sheet status to move and
        nothing in `project_charter` claiming them - the ledger is the
        only place they exist. Each is cancelled by its mirror: same
        date, same amount, sides swapped, original left as the trail.
        """
        AAL = self.env['account.analytic.line'].sudo()
        accounts = self._unproductive_projects().mapped('analytic_account_id')
        rows = self._live_wage_rows(accounts).filtered(
            # A row of 0.00 moves no money; mirroring it only adds two
            # more empty rows to the ledger.
            lambda r: not r.rcp_line_id and r.amount)
        log("rows with no RCP line: %d (%.2f)"
            % (len(rows), sum(abs(r.amount or 0.0) for r in rows)))

        # Mirrors written before the link existed carry the original's id
        # in their description and nothing else. Reconnect them, or the
        # rows they already cancelled keep being reported for ever.
        orphans = AAL.search([('name', 'like', MIRROR_MARKER),
                              ('rcp_reverse_of_id', '=', False)])
        relinked = 0
        for orphan in orphans:
            match = re.search(r'linia (\d+)', orphan.name or '')
            if not match:
                log("   cannot tell what mirror %s cancels - left alone"
                    % orphan.id)
                continue
            original = AAL.browse(int(match.group(1)))
            if not original.exists():
                log("   mirror %s names a row that no longer exists"
                    % orphan.id)
                continue
            orphan.rcp_reverse_of_id = original.id
            relinked += 1
        if orphans:
            log("mirrors missing their link: %d -> relinked %d"
                % (len(orphans), relinked))

        if relinked:
            # `rows` was read before the relink and still lists the rows
            # those mirrors cancel. Creating mirrors from that stale list
            # would correct the same money twice - which is exactly what
            # this step exists to avoid.
            self.env.flush_all()
            rows = self._live_wage_rows(accounts).filtered(
                lambda r: not r.rcp_line_id and r.amount)
            log("after relinking, rows still uncancelled: %d" % len(rows))
        if not rows:
            log("nothing left to cancel.")
            return

        created = AAL.with_context(
            allow_transfer_document_posting=True).create([{
                'name': "%s (linia %s)" % (MIRROR_MARKER, row.id),
                'date': row.date,
                'amount': -abs(row.amount),
                'unit_amount': 0.0,
                'employee_id': row.employee_id.id,
                # Sides swapped: what was charged is funded, and back.
                'account_id': row.destination_account_id.id,
                'destination_account_id': row.account_id.id,
                'company_id': row.company_id.id,
                'posting_origin': 'labor_absorption',
                # Record what this cancels. Without it the link lives
                # only in the description, where no query can follow it.
                'rcp_reverse_of_id': row.id,
            } for row in rows])
        # Tag both halves: the dashboard splits an employee account by
        # `posting_origin` reading the row whose account is the E- one,
        # and that row is the auto-generated counterpart, which inherits
        # nothing.
        halves = created | created.mapped('transfer_pair_id')
        halves.write({'posting_origin': 'labor_absorption'})
        log("created %d mirror(s) (%d rows incl. counterparts)"
            % (len(created), len(halves)))

    def _step_simulate(self, log):
        """Recompute the weeks for real, then unwind.

        `action_get_analytic_lines` gathers every approved hour of the
        week no RCP line has taken yet - on customer projects too.
        Running it and throwing the result away is the only way to see
        what it produces before agreeing to it, so this step never
        keeps anything, whatever the writing flag says.
        """
        self._require_scope()
        codes = self._scope_codes()
        documents = self._documents_for(codes)
        Lines = self.env['hr.rcp.lines'].sudo()
        existing = Lines.search([('rcp_id', 'in', documents.ids)])
        before = set(existing.ids)
        # Hours can also merge into a pending line, rewriting its
        # quantity and rate without creating anything - invisible to an
        # id diff.
        snapshot = {l.id: (l.mpk, l.quantity, l.unit_cost) for l in existing}

        log("documents in scope: %d" % len(documents))
        # A locked week refuses to recompute. The step is unwound at the
        # end, so this never reaches the database.
        locked = documents.filtered('locked')
        if locked:
            log("temporarily unlocking %d locked week(s)" % len(locked))
            locked.write({'locked': False})
        blocked = 0
        for document in documents:
            try:
                with self.env.cr.savepoint():
                    document.action_get_analytic_lines()
            except UserError as error:
                blocked += 1
                log("   SKIPPED %s: %s"
                    % (document.name or document.id, str(error).strip()[:110]))
        if blocked:
            log("   %d week(s) refuse to recompute - step 6 will skip them "
                "too" % blocked)

        added = Lines.search([('id', 'not in', sorted(before)),
                              ('rcp_id', 'in', documents.ids)])
        by_mpk = {}
        for line in added:
            entry = by_mpk.setdefault(line.mpk or '(none)',
                                      {'n': 0, 'cost': 0.0})
            entry['n'] += 1
            entry['cost'] += (line.quantity or 0.0) * (line.unit_cost or 0.0)
        log("")
        log("lines the recompute would add: %d" % len(added))
        for mpk in sorted(by_mpk, key=lambda m: (m not in codes, m)):
            log("   %-14s %4d lines %12.2f%s"
                % (mpk, by_mpk[mpk]['n'], by_mpk[mpk]['cost'],
                   '   <= in scope' if mpk in codes else '   <= OTHER PROJECT'))
        changed = [l for l in existing.exists()
                   if snapshot.get(l.id) != (l.mpk, l.quantity, l.unit_cost)]
        log("existing lines silently rewritten: %d" % len(changed))
        log("")
        log("SIMULATION - this step always unwinds, nothing is kept.")
        raise _DryRun()

    def _post_narrowed(self, documents, lines, log):
        """Post exactly these lines, and check the invariant afterwards.

        `action_post_to_analytic` would take every pending line of the
        week, and these weeks carry a backlog on other projects that is
        not ours to decide about.
        """
        for document in documents:
            mine = lines.filtered(lambda l, d=document: l.rcp_id == d)
            if mine:
                document._action_post_to_analytic_sync(line_ids=mine.ids)
        failed = lines.filtered(lambda l: l.status == 'error')
        for line in failed:
            log("   line %s: %s" % (line.id, line.analytic_last_error))
        # The whole point: a project that does not absorb must put no
        # wage on anybody's employee account.
        wages = self.env['account.analytic.line'].sudo().search([
            ('rcp_line_id', 'in', lines.ids),
            ('rcp_cmp_group', '=', False),
            ('rcp_operation_type', '=', 'post')])
        log("posted %d line(s); main wage lines created (must be 0): %d"
            % (len(lines) - len(failed), len(wages)))
        if wages or failed:
            raise UserError(_(
                "A wage was posted or a line failed - nothing was kept. "
                "%(wages)s wage line(s), %(failed)s failed line(s).",
                wages=len(wages), failed=len(failed)))

    def _step_overheads(self, log):
        """Put the overheads back, at the unproductive rate.

        Reversing took the legitimate A/Bx overheads with it - idle time
        really does cost them. The recompute rebuilds the lines at
        `a_cost + spec_cost` now the projects are flagged.
        """
        self._require_scope()
        codes = self._scope_codes()
        documents = self._documents_for(codes)
        Lines = self.env['hr.rcp.lines'].sudo()
        before = set(Lines.search([('rcp_id', 'in', documents.ids)]).ids)
        log("documents in scope: %d" % len(documents))

        relock = documents.filtered('locked')
        if relock:
            relock.write({'locked': False})
        # A week is recomputed whole, and these weeks carry lines for
        # other projects too. One of those in a closed stage makes
        # `action_get_analytic_lines` refuse the entire document - which
        # must not take the other weeks down with it. Nothing has been
        # deleted at this point, so skipping a blocked week is safe;
        # step 7 deliberately does not do this, because there a failed
        # recompute would leave the deleted lines gone.
        blocked = []
        for document in documents:
            try:
                with self.env.cr.savepoint():
                    document.action_get_analytic_lines()
            except UserError as error:
                blocked.append((document, str(error).strip()))
        for document, message in blocked:
            log("   SKIPPED %s: %s"
                % (document.name or document.id, message[:110]))
        if blocked:
            log("   %d week(s) could not be recomputed - see above"
                % len(blocked))

        rebuilt = Lines.search([('id', 'not in', sorted(before)),
                                ('rcp_id', 'in', documents.ids),
                                ('mpk', 'in', list(codes)),
                                ('status', '=', 'new')])
        log("rebuilt %d line(s), %.1f h, %.2f"
            % (len(rebuilt), sum(l.quantity or 0.0 for l in rebuilt),
               sum((l.quantity or 0.0) * (l.unit_cost or 0.0)
                   for l in rebuilt)))
        strays = Lines.search([('id', 'not in', sorted(before)),
                               ('rcp_id', 'in', documents.ids),
                               ('mpk', 'not in', list(codes))])
        if strays:
            log("left pending on other projects: %d line(s)" % len(strays))

        # Also post anything in scope still pending at the correct rate -
        # a reversal leaves those behind and they are the point.
        _pending, mispriced = self._mispriced_pending()
        skip = {line.id for line, _e in mispriced}
        leftover = Lines.search([('mpk', 'in', list(codes)),
                                 ('status', 'in', ('new', 'error')),
                                 ('id', 'not in', list(skip))])
        if skip:
            log("skipping %d line(s) priced at the productive rate - "
                "use step 7" % len(skip))
        to_post = rebuilt | leftover
        self._post_narrowed(to_post.mapped('rcp_id'), to_post, log)
        if relock:
            relock.write({'locked': True})

    def _step_reprice(self, log):
        """Delete pending lines priced productively and recompute them.

        A line written before its project was flagged carries `result`
        instead of a_cost + spec_cost, and the posting refuses it -
        rightly. It cannot be edited into shape either: merging hours
        rewrites a pending line's quantity but not a rate that is
        already frozen, so the only way back is to drop the line and let
        the sheet rebuild it at the rate the flag now implies.
        """
        self._require_scope()
        pending, wrong = self._mispriced_pending(log)
        log("pending lines in scope: %d, mispriced: %d"
            % (len(pending), len(wrong)))
        for line, expected in wrong[:20]:
            log("   line %-7s %s %6.1f h  states %8.2f  expects %8.2f"
                % (line.id, line.accounting_date, line.quantity or 0.0,
                   line.unit_cost or 0.0, expected))
        if not wrong:
            return

        Lines = self.env['hr.rcp.lines'].sudo()
        lines = Lines.browse([l.id for l, _e in wrong])
        documents = lines.mapped('rcp_id')
        before = set(Lines.search(
            [('rcp_id', 'in', documents.ids)]).ids) - set(lines.ids)

        relock = documents.filtered('locked')
        if relock:
            relock.write({'locked': False})
        lines.unlink()
        log("deleted %d mispriced line(s)" % len(wrong))
        for document in documents:
            document.action_get_analytic_lines()

        rebuilt = Lines.search([('id', 'not in', sorted(before)),
                                ('rcp_id', 'in', documents.ids),
                                ('mpk', 'in', list(self._scope_codes()))])
        log("recomputed %d line(s), %.1f h, %.2f"
            % (len(rebuilt), sum(l.quantity or 0.0 for l in rebuilt),
               sum((l.quantity or 0.0) * (l.unit_cost or 0.0)
                   for l in rebuilt)))
        # If the rebuild produced the same wrong rate the delete bought
        # nothing, and posting would refuse it all over again.
        _again, still_wrong = self._mispriced_pending()
        if still_wrong:
            raise UserError(_(
                "%s line(s) still carry the productive rate after the "
                "rebuild - nothing was kept.", len(still_wrong)))
        self._post_narrowed(documents, rebuilt, log)
        if relock:
            relock.write({'locked': True})

    def _step_verify(self, log, brief=False):
        total = self._step_audit(log, brief=brief)
        log("")
        if total:
            log("NOT CLEAN - %.2f still sits on employee accounts." % total)
        else:
            log("CLEAN - no non-absorbing project charges an employee "
                "account.")

    # ------------------------------------------------------------------
    # ledger integrity - the damage done by deleting posted lines
    # ------------------------------------------------------------------
    # Until hr_rcp 17.0.0.0.12 a posted RCP line could be deleted. Its
    # postings stayed, its hours were released, and the next recompute
    # booked them again - on db_dev that doubled 139 lines and 222
    # thousand PLN on the employee accounts. Reversals were stamped with
    # the day of the click, moving 577 thousand PLN from August into
    # September. These steps undo both without naming a single record:
    # every selection is a property of the ledger, so the same run is
    # right on any copy of the data.
    #
    # Shapes below, in SQL because they join the ledger to itself:
    #   orphan - a posting whose RCP line no longer exists, never reversed
    #   twin   - the same transfer (accounts, date, amount) posted from a
    #            line that still exists and is posted

    _ORPHAN_SQL = """
        split_part(o.rcp_move_key, ':', 2)::int NOT IN (SELECT id FROM hr_rcp_lines)
        AND o.amount <> 0
        AND NOT EXISTS (SELECT 1 FROM account_analytic_line r
                        WHERE r.rcp_reverse_of_id = o.id)"""
    _TWIN_SQL = """
        abs(c.amount - o.amount) < 0.02 AND c.date = o.date
        AND c.account_id = o.account_id
        AND c.destination_account_id = o.destination_account_id
        AND split_part(c.rcp_move_key, ':', 2)::int IN
            (SELECT id FROM hr_rcp_lines WHERE status = 'posted')"""
    _WAGE_KEY = r"^RCP:\d+:POST$"
    _CMP_KEY = r"^RCP:\d+:CMP:.*:POST$"

    def _sql_ids(self, query, params=None):
        self.env.flush_all()
        self.env.cr.execute(query, params or ())
        return [row[0] for row in self.env.cr.fetchall()]

    def _orphan_ids(self, key_pattern, twin=None, lines=None):
        """Orphans of one kind. `twin=True` keeps those with a twin,
        `False` those without; `lines` keeps those of these RCP line ids."""
        query = ("SELECT o.id FROM account_analytic_line o "
                 "WHERE o.rcp_move_key ~ %s AND " + self._ORPHAN_SQL)
        params = [key_pattern]
        if twin is not None:
            query += (" AND %sEXISTS (SELECT 1 FROM account_analytic_line c "
                      "WHERE c.rcp_move_key ~ %%s AND %s)"
                      % ('' if twin else 'NOT ', self._TWIN_SQL))
            params.append(key_pattern)
        if lines is not None:
            query += " AND split_part(o.rcp_move_key, ':', 2)::int = ANY(%s)"
            params.append(list(lines) or [0])
        return self._sql_ids(query + " ORDER BY o.id", params)

    def _log_by_month(self, log, label, rows):
        totals = {}
        for row in rows:
            month = row.date.strftime('%Y-%m')
            count, amount = totals.get(month, (0, 0.0))
            totals[month] = (count + 1, amount + (row.amount or 0.0))
        log("%s: %d row(s)" % (label, len(rows)))
        for month in sorted(totals):
            log("   %s  %6d  %14.2f" % ((month,) + totals[month]))

    def _reverse_ledger_rows(self, rows, reason):
        """The same reversal hr_rcp writes, for rows whose line is gone.

        Sides swapped, dated on the original, keyed like a native
        reversal so a second run finds it and skips the row. The origin
        comes from the pair when the source half has none: on the wage
        rows only the employee half was ever tagged, and an untagged
        reversal would leave the employee account looking unchanged.
        """
        AAL = self.env['account.analytic.line'].sudo().with_context(
            allow_transfer_document_posting=True)
        vals_list = []
        for row in rows:
            line_ref = row.rcp_move_key.split(':')[1]
            vals_list.append({
                'name': "Storno duplikatu po skasowanej linii RCP %s "
                        "(odwrócenie nr %s, %s)" % (line_ref, row.id, reason),
                'date': row.date,
                'amount': -abs(row.amount),
                'unit_amount': 0.0,
                'employee_id': row.employee_id.id,
                'account_id': row.destination_account_id.id,
                'destination_account_id': row.account_id.id,
                'company_id': row.company_id.id,
                'rcp_operation_type': 'reverse',
                'rcp_move_key': ("%s:REVERSE" % row.rcp_move_key
                                 if row.rcp_cmp_group else
                                 "RCP:%s:REVERSE:%s" % (line_ref, row.id)),
                'rcp_reverse_of_id': row.id,
                'rcp_cmp_group': row.rcp_cmp_group or False,
                'posting_origin': (row.posting_origin
                                   or row.transfer_pair_id.posting_origin
                                   or False),
            })
        created = AAL.browse()
        for start in range(0, len(vals_list), 500):
            created |= AAL.create(vals_list[start:start + 500])
        # A CMP storno keeps the sides of what it reverses: the new row sits
        # on the original's counterpart account, its pair on the original's
        # own account. The cost budget's full cost reads the side.
        opposite = {'charged': 'funded', 'funded': 'charged'}
        charged_ids, funded_ids = [], []
        for row, new in zip(rows, created):
            side = row.rcp_cmp_side or opposite.get(row.transfer_pair_id.rcp_cmp_side)
            if not row.rcp_cmp_group or not side:
                continue
            (charged_ids if opposite[side] == 'charged' else funded_ids).append(new.id)
            if new.transfer_pair_id:
                (charged_ids if side == 'charged' else funded_ids).append(new.transfer_pair_id.id)
        for side, ids in (('charged', charged_ids), ('funded', funded_ids)):
            if ids:
                self.env.cr.execute(
                    "UPDATE account_analytic_line SET rcp_cmp_side = %s WHERE id = ANY(%s)",
                    (side, ids))
        created.invalidate_recordset(['rcp_cmp_side'])
        return created

    def _step_pairs(self, log):
        """Give a one-sided RCP posting its other half back.

        Deleting the employee half of a wage transfer was accepted: the
        transfer layer marked the project half one-sided and the wage
        vanished from the employee account. Every RCP posting is a
        two-sided transfer, so a lone half is always this.
        """
        AAL = self.env['account.analytic.line'].sudo()
        rows = AAL.browse(self._sql_ids("""
            SELECT id FROM account_analytic_line
            WHERE rcp_move_key LIKE 'RCP:%%' AND destination_account_id IS NOT NULL
              AND transfer_pair_id IS NULL AND amount <> 0 ORDER BY id"""))
        self._log_by_month(log, "postings missing their other half", rows)
        for row in rows:
            vals = {'one_sided_operation': False}
            if not row.rcp_cmp_group:
                vals['posting_origin'] = 'labor_absorption'
            row.with_context(skip_transfer_pair_creation=True).write(vals)
            row._create_or_sync_counterpart(create_if_missing=True)
        if rows:
            log("restored %d other half(s)" % len(rows))

    def _step_orphans(self, log):
        """Reverse what deleted lines left behind and was booked again.

        Only with a twin: the same transfer still stands on a posted
        line, so the orphan is the second copy of money the ledger
        already has. The overheads of a duplicated wage go with it -
        they are the same hours. An orphan with no twin may be the only
        record of its hours and is reported, not touched.
        """
        AAL = self.env['account.analytic.line'].sudo()
        wages = AAL.browse(self._orphan_ids(self._WAGE_KEY, twin=True))
        self._log_by_month(log, "duplicated wage postings", wages)
        duplicated = {int(k.split(':')[1]) for k in wages.mapped('rcp_move_key')}
        overheads = AAL.browse(sorted(
            set(self._orphan_ids(self._CMP_KEY, lines=duplicated))
            | set(self._orphan_ids(self._CMP_KEY, twin=True))))
        self._log_by_month(log, "duplicated overhead postings", overheads)

        created = self._reverse_ledger_rows(wages, "wage")
        created |= self._reverse_ledger_rows(overheads, "overhead")
        if created:
            log("reversed %d row(s), each with its pair" % len(created))

        left = AAL.browse(self._orphan_ids(self._WAGE_KEY)
                          + self._orphan_ids(self._CMP_KEY))
        if left:
            self._log_by_month(
                log, "LEFT FOR REVIEW - orphans with no twin, not touched", left)

    def _step_redate(self, log):
        """Date every reversal on what it reverses, both halves."""
        AAL = self.env['account.analytic.line'].sudo()
        self.env.flush_all()
        self.env.cr.execute("""
            SELECT r.id, o.date FROM account_analytic_line r
            JOIN account_analytic_line o ON o.id = r.rcp_reverse_of_id
            WHERE r.rcp_operation_type = 'reverse' AND r.date <> o.date""")
        by_date = {}
        for reversal_id, original_date in self.env.cr.fetchall():
            by_date.setdefault(original_date, []).append(reversal_id)
        reversals = AAL.browse([i for ids in by_date.values() for i in ids])
        self._log_by_month(log, "reversals dated off their original", reversals)
        moved = 0
        for original_date, ids in by_date.items():
            halves = AAL.browse(ids)
            halves |= halves.mapped('transfer_pair_id')
            halves.write({'date': original_date})
            moved += len(halves)
        if moved:
            log("re-dated %d row(s) incl. pairs" % moved)

    def _step_link(self, log):
        """Attach an approved timesheet a posted line is missing.

        A timesheet re-created after posting gets a new id and drops out
        of the line that already booked its hours. Left free, the next
        recompute books it a second time. Attached only when the free
        timesheets of that employee, project and week add up to exactly
        the hours the line is short.
        """
        Lines = self.env['hr.rcp.lines'].sudo()
        AAL = self.env['account.analytic.line'].sudo()
        short = Lines.browse(self._sql_ids("""
            SELECT r.id FROM hr_rcp_lines r
            WHERE r.status = 'posted' AND r.analytic_posted_line_id IS NOT NULL
              AND r.quantity - COALESCE((
                    SELECT sum(t.unit_amount) FROM hr_rcp_lines_analytic_rel x
                    JOIN account_analytic_line t ON t.id = x.analytic_line_id
                    WHERE x.rcp_line_id = r.id), 0) > 0.01
            ORDER BY r.id"""))
        log("posted lines with fewer timesheet hours than they booked: %d"
            % len(short))
        for line in short:
            missing = line.quantity - sum(line.analytic_ids.mapped('unit_amount'))
            sheet = line.rcp_id
            free = AAL.search([
                ('employee_id', '=', line.description.id),
                ('account_id', '=', line.analytic_posted_line_id.account_id.id),
                ('project_id', '!=', False),
                ('date', '>=', sheet.request_monday_date or sheet.date),
                ('date', '<=', sheet.request_sunday_date or sheet.date),
                ('rcp_checked', '=', False),
            ]).filtered(lambda t: t.sheet_id.state == 'done')
            if free and abs(sum(free.mapped('unit_amount')) - missing) < 0.01:
                line.write({'analytic_ids': [(4, t.id) for t in free]})
                log("   line %s: attached %d timesheet(s), %.2f h"
                    % (line.id, len(free), missing))
            else:
                log("   line %s: %.2f h short, no exact match - LEFT FOR "
                    "REVIEW" % (line.id, missing))

    def _step_retry(self, log):
        """Post again what is in error, now that the posting can say
        "nothing to book" for a position without overhead."""
        Lines = self.env['hr.rcp.lines'].sudo()
        errors = Lines.search([('status', '=', 'error')])
        log("lines in error: %d" % len(errors))
        for line in errors:
            sheet = line.rcp_id.with_company(line.company_id)
            try:
                with self.env.cr.savepoint():
                    sheet._post_single_rcp_line_to_analytic(line)
            except UserError as error:
                log("   line %s: still refused - %s" % (line.id, error))
        still = errors.filtered(lambda l: l.status == 'error')
        log("posted now: %d, still in error: %d"
            % (len(errors) - len(still), len(still)))

    def _step_reconcile(self, log):
        """Sheet against ledger, month by month, both sides of the wage.

        Employee side: hours x `basic_cost` of the posted lines against
        the wage on E- accounts. Project side: hours x `unit_cost`
        against what the project's account carries, overheads included,
        with the automatic halves joined in - they carry no move key.
        Returns the employee-side difference, the figure that has to be
        zero.
        """
        cr = self.env.cr
        self.env.flush_all()
        cr.execute("""
            WITH expected AS (
                SELECT to_char(COALESCE(r.accounting_date, h.date), 'YYYY-MM') m,
                       sum(round((r.quantity * pl.basic_cost)::numeric, 2)) amount
                FROM hr_rcp_lines r JOIN hr_rcp h ON h.id = r.rcp_id
                JOIN account_analytic_line w ON w.id = r.analytic_posted_line_id
                JOIN cmp_personnel_costs_model pm
                  ON pm.date_month = date_trunc('month', COALESCE(r.accounting_date, h.date))::date
                 AND pm.company_id = h.company_id
                JOIN cmp_personnel_costs_line pl
                  ON pl.personnel_id = pm.id AND pl.employee = r.description
                WHERE r.status = 'posted'
                  AND w.account_id IS DISTINCT FROM w.destination_account_id
                GROUP BY 1),
            booked AS (
                SELECT to_char(l.date, 'YYYY-MM') m, sum(l.amount) amount
                FROM account_analytic_line l
                JOIN account_analytic_account a ON a.id = l.account_id
                LEFT JOIN account_analytic_line p ON p.id = l.transfer_pair_id
                LEFT JOIN hr_employee emp ON emp.id = COALESCE(l.employee_id, p.employee_id)
                WHERE l.posting_origin = 'labor_absorption'
                  AND (a.code LIKE 'E-%%' OR l.account_id = emp.account_id)
                  AND NOT EXISTS (SELECT 1 FROM account_analytic_line p
                                   WHERE p.id = l.transfer_pair_id
                                     AND p.account_id = l.account_id)
                GROUP BY 1)
            SELECT COALESCE(e.m, b.m), COALESCE(e.amount, 0), COALESCE(b.amount, 0)
            FROM expected e FULL JOIN booked b ON b.m = e.m ORDER BY 1""")
        rows = cr.fetchall()
        log("employee accounts      expected        booked    difference")
        off = 0.0
        for month, expected, booked in rows:
            difference = (booked or 0.0) - (expected or 0.0)
            if abs(difference) >= 0.01:
                off += abs(difference)
            log("   %s  %14.2f %14.2f %12.2f%s" % (
                month, expected, booked, difference,
                '' if abs(difference) < 0.01 else '  <-'))

        # A posted line whose sheet no longer says what the ledger
        # booked - quantity rewritten before the freeze existed. Which
        # side is right depends on the timesheets, so it is listed for a
        # person to settle (reverse, then a correction line), not guessed.
        cr.execute("""
            SELECT r.id, h.name, r.mpk, r.quantity,
                   round((r.quantity * pl.basic_cost)::numeric, 2), abs(w.amount)
            FROM hr_rcp_lines r JOIN hr_rcp h ON h.id = r.rcp_id
            JOIN account_analytic_line w ON w.id = r.analytic_posted_line_id
            JOIN cmp_personnel_costs_model pm
              ON pm.date_month = date_trunc('month', COALESCE(r.accounting_date, h.date))::date
             AND pm.company_id = h.company_id
            JOIN cmp_personnel_costs_line pl
              ON pl.personnel_id = pm.id AND pl.employee = r.description
            WHERE r.status = 'posted'
              AND w.account_id IS DISTINCT FROM w.destination_account_id
              AND abs(abs(w.amount) - abs(round((r.quantity * pl.basic_cost)::numeric, 2))) >= 0.01
            ORDER BY r.id""")
        drift = cr.fetchall()
        if drift:
            log("LEFT FOR REVIEW - posted lines whose sheet disagrees with "
                "the ledger: %d" % len(drift))
            for line_id, sheet, mpk, quantity, sheet_amount, ledger in drift[:30]:
                log("   line %-7s %-26s %-9s %8.2f h  sheet %10.2f  ledger %10.2f"
                    % (line_id, sheet, mpk, quantity, sheet_amount, ledger))

        cr.execute("""
            SELECT
              (SELECT count(*) FROM account_analytic_line r
               JOIN account_analytic_line o ON o.id = r.rcp_reverse_of_id
               WHERE r.rcp_operation_type = 'reverse' AND r.date <> o.date),
              (SELECT count(*) FROM account_analytic_line
               WHERE rcp_move_key LIKE 'RCP:%%' AND destination_account_id IS NOT NULL
                 AND transfer_pair_id IS NULL AND amount <> 0),
              (SELECT count(*) FROM (SELECT rcp_move_key FROM account_analytic_line
               WHERE rcp_move_key LIKE 'RCP:%%' GROUP BY 1 HAVING count(*) > 1) z),
              (SELECT count(*) FROM hr_rcp_lines WHERE status = 'error')""")
        dated, lone, keys, errors = cr.fetchone()
        log("reversals off their date: %d, one-sided postings: %d, "
            "duplicate keys: %d, lines in error: %d"
            % (dated, lone, keys, errors))
        log("")
        log("LEDGER %s" % ("CLEAN - the employee accounts agree with RCP "
                           "in every month" if not off else
                           "NOT CLEAN - %.2f apart, months marked <-" % off))
        return off

    def _account_references(self):
        """(table, column) of every foreign key to an analytic account."""
        self.env.cr.execute("""
            SELECT tc.table_name, kcu.column_name
              FROM information_schema.table_constraints tc
              JOIN information_schema.key_column_usage kcu
                ON kcu.constraint_name = tc.constraint_name
              JOIN information_schema.constraint_column_usage ccu
                ON ccu.constraint_name = tc.constraint_name
             WHERE tc.constraint_type = 'FOREIGN KEY'
               AND ccu.table_name = 'account_analytic_account'
               AND ccu.column_name = 'id'
               AND tc.table_name <> 'account_analytic_account'""")
        return self.env.cr.fetchall()

    def _move_account_references(self, target, sources, references):
        """Point everything that names `sources` at `target`; {where: rows}.

        In SQL, because posted RCP rows refuse an ORM write by design; the
        move changes where a row sits, not what it says.
        """
        cr = self.env.cr
        moved = {}
        if not sources:
            return moved
        if 'project.invoice.flow' in self.env:
            # One approval row per account and Enterprise invoice: an
            # invoice split over two merged accounts has a row on each,
            # and after the merge they would be the same row.
            cr.execute("""
                SELECT f.id FROM project_invoice_flow f
                 WHERE f.analytic_account_id = ANY(%s)
                   AND EXISTS (SELECT 1 FROM project_invoice_flow g
                                WHERE g.external_move_id = f.external_move_id
                                  AND g.id <> f.id
                                  AND (g.analytic_account_id = %s
                                       OR (g.analytic_account_id = ANY(%s) AND g.id < f.id)))
            """, (sources.ids, target.id, sources.ids))
            twins = self.env['project.invoice.flow'].sudo().browse(
                [row[0] for row in cr.fetchall()])
            if twins:
                twins.unlink()
                moved['project_invoice_flow twins deleted'] = len(twins)
        for table, column in references:
            if table in SELECTION_RELATIONS:
                cr.execute('DELETE FROM "%s" WHERE "%s" = ANY(%%s)' % (table, column),
                           (sources.ids,))
                continue
            cr.execute('UPDATE "%s" SET "%s" = %%s WHERE "%s" = ANY(%%s)'
                       % (table, column, column), (target.id, sources.ids))
            if cr.rowcount:
                moved['%s.%s' % (table, column)] = cr.rowcount
        self.env.invalidate_all()
        return moved

    def _step_merge_person_accounts(self, log):
        """Put the sales department on the accounts decided on 06-07.10.2026.

        - every A038-E-... person (sales person or sales support) has that
          one account: their E-account is folded into it;
        - a sales person's offer project books on it too - its own
          account is folded in and the project repointed; a sales person
          without the account (Janusz Rudnicki) gets one by the standard;
        - the general offer project 26-0039 books on A038;
        - the folded accounts are deleted, projects of people who left
          archived, and the A038-E accounts of people who left archived.

        A transfer between two merged accounts has both halves on one
        account and nets to zero there - the pay of hours spent on one's
        own offers stays with them (hr_rcp _absorbs_no_wage).
        """
        self.env.flush_all()
        Account = self.env['account.analytic.account'].sudo().with_context(active_test=False)
        Employee = self.env['hr.employee'].sudo().with_context(active_test=False)
        Project = self.env['project.project'].sudo().with_context(active_test=False)
        references = self._account_references()
        to_delete = Account.browse()

        # Sales people missing their account get it first.
        for identifier in sorted(set(OFFER_PROJECTS.values())):
            employee = Employee.search([('unique_identifier', '=', identifier)])
            if len(employee) != 1:
                log("   LEFT FOR REVIEW %s: %d employees with this identifier"
                    % (identifier, len(employee)))
                continue
            code = employee._get_function_account_code(MERGED_COST_CENTRES[0])
            if not Account.search([('code', '=', code)]):
                centre = Account.search([('code', '=', MERGED_COST_CENTRES[0])], limit=1)
                Account.create({
                    'name': _('Offers - %s') % employee.name, 'code': code,
                    'plan_id': (centre.plan_id or employee.account_id.plan_id).id,
                    'company_id': employee.company_id.id,
                })
                log("   created %s" % code)

        targets = Account.search(['|'] * (len(MERGED_COST_CENTRES) - 1) + [
            ('code', '=like', '%s-E-%%' % code) for code in MERGED_COST_CENTRES], order='code')
        merged_projects = Project.browse()
        for target in targets:
            e_code = target.code[5:]
            identifier = e_code.split('-')[1] if e_code.count('-') >= 2 else False
            source_e = Account.search([('code', '=', e_code)])
            employees = Employee.search([('account_id', 'in', source_e.ids)]) if source_e else \
                Employee.search([('unique_identifier', '=', identifier),
                                 ('account_id', 'not in', targets.ids)])
            if len(employees) > 1:
                employees = employees.filtered(lambda e: e.account_id) or employees
            if len(employees) > 1:
                log("   LEFT FOR REVIEW %s: %d employees (%s) - which one is it?"
                    % (target.code, len(employees), ', '.join(employees.mapped('name'))))
                continue
            projects = Project.search([
                ('code', 'in', [c for c, ident in OFFER_PROJECTS.items() if ident == identifier]),
                ('analytic_account_id', '!=', target.id)])
            sources = source_e | projects.mapped('analytic_account_id')
            moved = self._move_account_references(target, sources, references)
            if employees and employees.account_id != target:
                employees.write({'account_id': target.id})
                moved['hr_employee.account_id'] = len(employees)
            merged_projects |= projects
            to_delete |= sources
            if moved:
                log("   %-26s <- %-26s %s" % (
                    target.code, ', '.join(sources.mapped('code')) or '-',
                    ', '.join('%s %d' % item for item in sorted(moved.items()))))

        for code, centre_code in GENERAL_OFFER_PROJECTS.items():
            project = Project.search([('code', '=', code)])
            # A code is not unique: A046 also names six archived 2023 team
            # accounts. The centre is the one account still in use.
            centre = Account.search([('code', '=', centre_code)])
            if len(centre) > 1:
                centre = centre.filtered('active')
            if len(project) != 1 or len(centre) != 1:
                log("   LEFT FOR REVIEW general project %s / %s: not found once" % (code, centre_code))
                continue
            sources = project.analytic_account_id - centre
            moved = self._move_account_references(centre, sources, references)
            to_delete |= sources
            # Enterprise may keep booking under the old cost centre code
            # (A011-1); an override sends those costs to the centre.
            Override = self.env['sync.cost.center.account.mapping'].sudo().with_context(
                active_test=False)
            for source in sources.filtered(lambda a: (a.code or '').upper().startswith('A')):
                if not Override.search_count([('source_code', '=', source.code.upper())]):
                    Override.create({'source_code': source.code, 'analytic_account_id': centre.id,
                                     'note': 'mj_rcp_repair: %s merged into %s' % (
                                         source.code, centre.code)})
                    moved['override %s -> %s' % (source.code, centre.code)] = 1
            if moved:
                log("   %-26s <- %-26s %s" % (centre.code, ', '.join(sources.mapped('code')),
                                              ', '.join('%s %d' % item for item in sorted(moved.items()))))

        for centre_code, patterns in CENTRE_ACCOUNT_FOLDS.items():
            centre = Account.search([('code', '=', centre_code)])
            if len(centre) > 1:
                centre = centre.filtered('active')
            if len(centre) != 1:
                log("   LEFT FOR REVIEW centre %s: %d accounts with this code"
                    % (centre_code, len(centre)))
                continue
            domain = ['|'] * (len(patterns) - 1) + [
                ('code', '=like', pattern) for pattern in patterns]
            sources = Account.search(domain) - centre - to_delete
            if not sources:
                continue
            moved = self._move_account_references(centre, sources, references)
            to_delete |= sources
            Override = self.env['sync.cost.center.account.mapping'].sudo().with_context(
                active_test=False)
            for source in sources.filtered(lambda a: (a.code or '').upper().startswith('A')):
                if not Override.search_count([('source_code', '=', source.code.upper())]):
                    Override.create({'source_code': source.code, 'analytic_account_id': centre.id,
                                     'note': 'mj_rcp_repair: %s merged into %s' % (
                                         source.code, centre.code)})
                    moved['override %s -> %s' % (source.code, centre.code)] = 1
            if not centre.active:
                centre.write({'active': True})
            log("   %-26s <- %-26s %s" % (centre.code, ', '.join(sources.mapped('code')),
                                          ', '.join('%s %d' % item for item in sorted(moved.items()))))

        if merged_projects:
            # The person's own hours there now move no wage (hr_rcp
            # _absorbs_no_wage); lines not posted yet get that rate.
            pending = self.env['hr.rcp.lines'].sudo().search([
                ('mpk', 'in', merged_projects.mapped('code')),
                ('status', 'in', ('new', 'error'))])
            repriced = 0
            for line in pending.filtered('description'):
                project = merged_projects.filtered(lambda p: p.code == line.mpk)[:1]
                if not line.rcp_id._absorbs_no_wage(line.description, project):
                    continue
                try:
                    rate = line.rcp_id._rcp_unit_cost(line.description, project)
                except UserError as error:
                    log("   LEFT FOR REVIEW line %s: %s" % (line.id, error))
                    continue
                if abs((line.unit_cost or 0.0) - rate) > RATE_TOLERANCE:
                    line.write({'unit_cost': rate})
                    repriced += 1
            log("offer projects merged: %s; own pending lines repriced: %d"
                % (', '.join(merged_projects.mapped('code')), repriced))

        self.env.flush_all()
        deleted = 0
        for account in to_delete:
            if self.env['account.analytic.line'].sudo().search_count(
                    [('account_id', '=', account.id)]):
                log("   LEFT FOR REVIEW %s: still has analytic lines - not deleted"
                    % account.code)
                continue
            code = account.code
            account.unlink()
            deleted += 1
            log("   deleted %s" % code)

        archived = []
        for project in Project.search([('code', 'in', ARCHIVED_PROJECTS), ('active', '=', True)]):
            project.write({'active': False})
            archived.append(project.code)
            account = project.analytic_account_id
            if account.active and not Project.search_count(
                    [('analytic_account_id', '=', account.id), ('active', '=', True)]) \
                    and not Employee.search_count([('account_id', '=', account.id)]):
                account.write({'active': False})
                archived.append(account.code)
        for identifier in LEFT_SALES_STAFF:
            employee = Employee.search([('unique_identifier', '=', identifier)], limit=1)
            account = employee.account_id
            if not account.active:
                continue
            # Archived on purpose (decision of 07.10.2026): whatever Enterprise
            # still books for them will not reach CE - said, not prevented.
            left = employee._get_departure_date()
            if left and not self.env['account.analytic.line'].sudo().search_count([
                    ('account_id', '=', account.id), ('posting_origin', '=', 'payroll'),
                    ('date', '>=', left.replace(day=1))]):
                log("   NOTE %s: no payroll for %s (left %s) in CE - anything Enterprise "
                    "still books for it will not be synchronised" % (
                        account.code, left.strftime('%m/%Y'), left))
            Project.search([('analytic_account_id', '=', account.id),
                            ('active', '=', True)]).write({'active': False})
            account.write({'active': False})
            archived.append(account.code)
        if archived:
            log("   archived: %s" % ', '.join(archived))
        log("person accounts: %d, accounts deleted: %d" % (len(targets), deleted))

    def _step_sheet_matches_ledger(self, log):
        """Give a posted line back the quantity its posting was made for.

        A quantity rewritten on a posted line before the freeze existed
        (54489: posted for 16 h, rewritten to -4 h on 22.09) leaves sheet and
        ledger telling different stories, and its timesheets lose their
        link while keeping the "taken" flag. The ledger is what was booked,
        so the sheet is put back to it - only where the ledger amount comes
        to a whole number of quarter hours at the line's rate - and the
        timesheets of that week, employee and project that are taken by no
        line are attached to it again. What the timesheets say now is then
        settled by the next step with an ordinary correction line.
        """
        cr = self.env.cr
        self.env.flush_all()
        cr.execute("""
            SELECT r.id, r.quantity, pl.basic_cost, w.amount,
                   (a.code LIKE 'E-%%' OR a.id = (SELECT e.account_id FROM hr_employee e
                                                   WHERE e.id = r.description)) AS inverted
              FROM hr_rcp_lines r JOIN hr_rcp h ON h.id = r.rcp_id
              JOIN account_analytic_line w ON w.id = r.analytic_posted_line_id
              JOIN account_analytic_account a ON a.id = w.account_id
              JOIN cmp_personnel_costs_model pm
                ON pm.date_month = date_trunc('month', COALESCE(r.accounting_date, h.date))::date
               AND pm.company_id = h.company_id
              JOIN cmp_personnel_costs_line pl
                ON pl.personnel_id = pm.id AND pl.employee = r.description
             WHERE r.status = 'posted' AND h.date >= %s AND pl.basic_cost > 0
               AND w.account_id IS DISTINCT FROM w.destination_account_id
               AND abs(abs(w.amount) - abs(round((r.quantity * pl.basic_cost)::numeric, 2))) >= 0.01
        """, (SYNC_DATE_FROM,))
        Line = self.env['hr.rcp.lines'].sudo()
        AAL = self.env['account.analytic.line'].sudo()
        fixed = 0
        for line_id, quantity, rate, amount, inverted in cr.fetchall():
            hours = abs(amount) / rate * (-1 if inverted else 1)
            if abs(hours * 4 - round(hours * 4)) > 0.01:
                log("   LEFT FOR REVIEW line %s: ledger %.2f is not a whole number "
                    "of quarter hours at %.2f" % (line_id, amount, rate))
                continue
            hours = round(hours * 4) / 4
            line = Line.browse(line_id)
            sheet = line.rcp_id
            project = self.env['project.project'].with_context(
                active_test=False).search([('code', '=', line.mpk)])
            orphans = AAL.browse()
            if len(project) == 1:
                cr.execute("""
                    SELECT l.id FROM account_analytic_line l
                     WHERE l.employee_id = %s AND l.project_id = %s
                       AND l.rcp_checked AND l.date BETWEEN %s AND %s
                       AND NOT EXISTS (SELECT 1 FROM hr_rcp_lines_analytic_rel x
                                        WHERE x.analytic_line_id = l.id)""",
                    (line.description.id, project.id,
                     max(sheet.request_monday_date, sheet.first_day_of_month or sheet.request_monday_date),
                     min(sheet.request_sunday_date, sheet.last_day_of_month or sheet.request_sunday_date)))
                orphans = AAL.browse([row[0] for row in cr.fetchall()])
            # SQL for the quantity: the freeze on posted lines exists to stop
            # exactly the edit being undone here.
            cr.execute("UPDATE hr_rcp_lines SET quantity = %s WHERE id = %s",
                       (hours, line_id))
            line.invalidate_recordset(['quantity'])
            if orphans:
                line.write({'analytic_ids': [(4, al.id) for al in orphans]})
            fixed += 1
            log("   line %s %s: sheet %.2f h -> %.2f h as posted%s"
                % (line_id, line.mpk, quantity, hours,
                   (", %d timesheet line(s) re-attached" % len(orphans)) if orphans else ""))
        log("lines put back: %d" % fixed)

    def _step_duplicate_lines(self, log):
        """Delete the unposted lines that copy another line of the sheet.

        On 05.10.2026 at 10:31 one operation added 3 355 lines to every
        2026 sheet: each a copy of a line already there, either holding
        the same timesheets (10 010 timesheet lines ended up taken twice)
        or, with no timesheets, repeating a posted or reversed line hour
        for hour - week 33 alone carried 345 335 zł of them. Posted, they
        cost the hours twice; left unposted, the hours step reads them as
        hours the sheet booked (a -88 h correction where -44 h was due).

        Only a line nothing was posted from is touched, and only when it
        is a copy of an OLDER line: every timesheet it holds is held by an
        older live line of the sheet, or it has no timesheets and an older
        posted or reversed line has the same employee, MPK and hours.
        Whatever the hours step then finds missing - the copy carried the
        hours its original had been lowered to zero for - it books.
        """
        cr = self.env.cr
        self.env.flush_all()
        cr.execute("""
            WITH candidate AS (
                SELECT r.id, r.rcp_id, r.description, r.mpk, r.quantity
                  FROM hr_rcp_lines r JOIN hr_rcp h ON h.id = r.rcp_id
                 WHERE r.status = 'new' AND h.date >= %s
                   AND NOT EXISTS (SELECT 1 FROM account_analytic_line l
                                    WHERE l.rcp_line_id = r.id))
            SELECT c.id FROM candidate c
             WHERE (EXISTS (SELECT 1 FROM hr_rcp_lines_analytic_rel x
                             WHERE x.rcp_line_id = c.id)
                    AND NOT EXISTS (
                        SELECT 1 FROM hr_rcp_lines_analytic_rel x
                         WHERE x.rcp_line_id = c.id
                           AND NOT EXISTS (
                               SELECT 1 FROM hr_rcp_lines_analytic_rel y
                                 JOIN hr_rcp_lines o ON o.id = y.rcp_line_id
                                WHERE y.analytic_line_id = x.analytic_line_id
                                  AND o.rcp_id = c.rcp_id AND o.id < c.id
                                  AND o.status <> 'reversed')))
                OR (NOT EXISTS (SELECT 1 FROM hr_rcp_lines_analytic_rel x
                                 WHERE x.rcp_line_id = c.id)
                    AND c.quantity <> 0
                    AND EXISTS (
                        SELECT 1 FROM hr_rcp_lines o
                         WHERE o.rcp_id = c.rcp_id AND o.id < c.id
                           AND o.description = c.description AND o.mpk = c.mpk
                           AND o.quantity = c.quantity
                           AND o.status IN ('posted', 'reversed')))
             ORDER BY c.id""", (SYNC_DATE_FROM,))
        lines = self.env['hr.rcp.lines'].sudo().browse([row[0] for row in cr.fetchall()])
        by_sheet = {}
        for line in lines:
            entry = by_sheet.setdefault(line.rcp_id, [0, 0.0, 0.0])
            entry[0] += 1
            entry[1] += line.quantity
            entry[2] += line.quantity * line.unit_cost
        for sheet, (count, hours, amount) in sorted(
                by_sheet.items(), key=lambda item: item[0].date):
            log("   %-26s %4d line(s) %9.2f h %13.2f" % (sheet.name, count, hours, amount))
        # unlink releases nothing here - the originals still hold the
        # timesheets - but goes through _sync_rcp_checked all the same.
        lines.unlink()
        log("duplicate lines deleted: %d" % len(lines))

    def _step_sheet_totals(self, log):
        """Recompute the totals of every sheet and list the unbalanced.

        The result of a sheet is costs minus funding minus unproductive
        cost; with the funding read from the ledger it tends to zero, so
        what is still off afterwards is a real difference, not a stale
        figure (the stored ones of 05.10.2026 were doubled).
        """
        sheets = self.env['hr.rcp'].sudo().search([], order='date, id')
        for sheet in sheets:
            sheet.with_company(sheet.company_id)._recompute_sheet_totals()
        self.env.flush_all()
        off = sheets.filtered(lambda sheet: abs(sheet.result) > SHEET_RESULT_TOLERANCE)
        for sheet in off:
            log("   LEFT FOR REVIEW %-26s costs %13.2f  funding %13.2f  "
                "unproductive %11.2f  result %11.2f"
                % (sheet.name, sheet.total_costs, sheet.total_benefits,
                   sheet.total_unproductive, sheet.result))
        log("sheets recomputed: %d, result above %.0f zł: %d"
            % (len(sheets), SHEET_RESULT_TOLERANCE, len(off)))

    def _step_unbook_cost_centre_staff(self, log):
        """Reverse every 2026 RCP posting of the people of UNBOOKED_COST_CENTRES.

        Their hours stop charging projects; the timesheets themselves stay
        (decision of 14.10.2026). A reversal releases the timesheets it
        held, and hr.rcp would take them again on the next computation of
        the sheet - so these people are first marked rcp_excluded (not
        "production": that flag also counts them in the cmp_costs divisor
        of overhead rates). Lines not posted yet are set aside without a
        posting. Runs on locked weeks and closed projects too - the
        reversal is the ledger catching up with a decision. Safe to run
        again: only posted and pending lines are touched.
        """
        Employee = self.env['hr.employee'].sudo().with_context(active_test=False)
        domain = ['|'] * (len(UNBOOKED_COST_CENTRES) - 1) + [
            ('account_id.code', '=like', '%s-E-%%' % code) for code in UNBOOKED_COST_CENTRES]
        employees = Employee.search(domain)
        if not employees:
            log("nobody on %s" % ', '.join(UNBOOKED_COST_CENTRES))
            return
        newly_excluded = employees.filtered(lambda e: not e.rcp_excluded)
        if newly_excluded:
            newly_excluded.write({'rcp_excluded': True})
            log("   excluded from RCP: %s" % ', '.join(sorted(newly_excluded.mapped('name'))))
        lines = self.env['hr.rcp.lines'].sudo().search([
            ('description', 'in', employees.ids),
            ('accounting_date', '>=', SYNC_DATE_FROM),
            ('status', 'in', ('posted', 'new', 'error')),
        ], order='id')
        totals = {}
        for line in lines:
            # Closed projects too: both guards of project_timesheet_control
            # step aside, as they do for the closure's own postings.
            ctx = {'skip_project_freeze_guard': True, 'skip_financial_close_lock': True}
            line = line.with_context(**ctx)
            sheet = line.rcp_id.sudo().with_company(line.rcp_id.company_id).with_context(**ctx)
            was_posted = line.status == 'posted'
            if was_posted:
                state, _result = sheet._reverse_single_rcp_line(line)
                if state not in ('reversed', 'skipped'):
                    log("   LEFT FOR REVIEW line %s: %s" % (line.id, state))
                    continue
            else:
                line.write({'status': 'reversed', 'analytic_last_error': False})
            name = line.description.name
            count, hours, posted = totals.get(name, (0, 0.0, 0))
            totals[name] = (count + 1, hours + (line.quantity or 0.0), posted + int(was_posted))
        for name in sorted(totals):
            count, hours, posted = totals[name]
            log("   %-26s %4d line(s), %8.2f h  (%d reversed, %d set aside)"
                % (name, count, hours, posted, count - posted))
        log("lines taken out of RCP: %d" % sum(t[0] for t in totals.values()))

    def _step_hours_follow_timesheets(self, log):
        """Book the hours timesheets lost or moved after RCP took them.

        hr.rcp._reconcile_taken_hours (hr_rcp 17.0.0.0.17) compares, per
        employee and MPK, the hours a sheet booked with the timesheets it
        took, and writes a correction line for the difference. Here it is
        run over every 2026 sheet and the corrections are posted at once,
        so a project charged for hours re-entered on another one (25-0061
        instead of 26-0136, 12.08-23.09.2026) is relieved of them in the
        ledger, not just on paper. Locked weeks included: the correction
        is the ledger catching up with timesheets, not an edit of the week.
        """
        Rcp = self.env['hr.rcp'].sudo()
        if not hasattr(Rcp, '_reconcile_taken_hours'):
            log("hr_rcp has no _reconcile_taken_hours - upgrade hr_rcp first.")
            return
        sheets = Rcp.search([('date', '>=', SYNC_DATE_FROM)], order='date, id')
        total_lines = total_hours = 0.0
        for sheet in sheets:
            sheet = sheet.with_company(sheet.company_id)
            before = set(sheet.rcp_lines.mapped('analytic_ids').ids)
            corrections, skipped = sheet._reconcile_taken_hours()
            released = len(before - set(sheet.rcp_lines.mapped('analytic_ids').ids))
            for employee, mpk in skipped:
                correction = self._zero_line_without_timesheets(sheet, employee, mpk)
                if correction:
                    corrections |= correction
                    continue
                log("   LEFT FOR REVIEW %s %s / %s: a line without timesheets"
                    % (sheet.name, employee, mpk))
            if released:
                log("   %s: %d timesheet line(s) moved to another project released "
                    "- compute the sheet to book them there" % (sheet.name, released))
            for line in corrections:
                state, _wage = sheet._post_single_rcp_line_to_analytic(line)
                total_lines += 1
                total_hours += line.quantity
                log("   %s %-24s %-9s %8.2f h  -> %s"
                    % (sheet.name, line.description.name, line.mpk, line.quantity, state))
        # Funding totals are read from the ledger; every sheet this run may
        # have posted to is brought up to date.
        for sheet in sheets:
            sheet.with_company(sheet.company_id)._recompute_sheet_totals()
        log("corrections posted: %d (%.2f h)" % (total_lines, total_hours))

    def _zero_line_without_timesheets(self, sheet, employee_name, mpk):
        """A posted line that lost every timesheet (the timesheets were
        deleted) is taken back to zero - but only when the employee has no
        approved hours on that project in the sheet's period at all, so no
        timesheet can be the one it was costed from.
        """
        Project = self.env['project.project'].with_context(active_test=False)
        project = Project.search([('code', '=', mpk)])
        if len(project) != 1:
            return self.env['hr.rcp.lines']
        group = sheet.rcp_lines.filtered(
            lambda ln: ln.mpk == mpk and ln.status != 'reversed'
            and ln.description.name == employee_name)
        employee = group[:1].description
        if not employee or group.mapped('analytic_ids') or not group.filtered(
                lambda ln: ln.status == 'posted'):
            return self.env['hr.rcp.lines']
        start = max(sheet.request_monday_date, sheet.first_day_of_month or sheet.request_monday_date)
        end = min(sheet.request_sunday_date, sheet.last_day_of_month or sheet.request_sunday_date)
        hours = sum(self.env['account.analytic.line'].sudo().search([
            ('employee_id', '=', employee.id), ('project_id', '=', project.id),
            ('date', '>=', start), ('date', '<=', end),
            ('sheet_state', '=', 'done'),
        ]).mapped('unit_amount'))
        booked = sum(group.mapped('quantity'))
        if hours or not booked:
            return self.env['hr.rcp.lines']
        template = group.sorted('id')[0]
        return self.env['hr.rcp.lines'].sudo().create({
            'status': 'new', 'is_correction': True, 'hours_reconciliation': True,
            'description': employee.id, 'doc_num': template.doc_num,
            'accounting_date': template.accounting_date, 'mpk': mpk,
            'order_num': template.order_num, 'quantity': -booked,
            'unit_cost': sheet._rcp_unit_cost(employee, project),
            'economic_group': template.economic_group, 'rcp_id': sheet.id,
        })

    def _run_ledger(self, log):
        """The ledger steps, in the order they depend on each other.

        Pairs first: a lone half has to be whole before it can be
        recognised as somebody's twin. Duplicates next, their reversals
        written on the original's date - so redating afterwards only
        has the older reversals left to move.
        """
        for title, step in (("sales person accounts merged", self._step_merge_person_accounts),
                            ("cost centre staff taken out of RCP", self._step_unbook_cost_centre_staff),
                            ("one-sided postings", self._step_pairs),
                            ("duplicates of deleted lines", self._step_orphans),
                            ("reversal dates", self._step_redate),
                            ("timesheets missing from posted lines", self._step_link),
                            ("lines in error", self._step_retry),
                            ("sheet quantity rewritten after posting", self._step_sheet_matches_ledger),
                            ("unposted copies of other lines", self._step_duplicate_lines),
                            ("hours changed after posting", self._step_hours_follow_timesheets),
                            ("sheet totals", self._step_sheet_totals),
                            ("reconcile", self._step_reconcile)):
            log("LEDGER  %s" % title)
            log("-" * 58)
            step(log)
            log("")

    # ------------------------------------------------------------------
    # analytics: what deploying the account standard needs once
    # ------------------------------------------------------------------
    def _run_analytics(self, log):
        """The steps that follow deploying the cost centre account standard
        and payroll matched by account code, done once, in this order.

        The crons only pick up Enterprise lines written since their last
        run, so history recoded in Enterprise is fetched here, in full.
        Each sync runs in its own savepoint: Enterprise being unreachable
        is reported and the ledger repair above is kept.
        """
        log("ANALYTICS  RCP week documents missing for part of a month")
        log("-" * 58)
        self._step_missing_rcp_weeks(log)
        log("")

        log("ANALYTICS  project numbers and their accounts")
        log("-" * 58)
        self._step_project_codes(log)
        log("")

        log("ANALYTICS  project accounts wearing a cost centre code")
        log("-" * 58)
        self._step_project_account_codes(log)
        log("")

        log("ANALYTICS  per-person cost centre accounts missing in CE")
        log("-" * 58)
        try:
            with self.env.cr.savepoint():
                self._step_person_accounts(log)
        except UserError as error:
            log("SKIPPED: %s" % error)
        log("")

        for title, service, method in (
                ("cost centre sync (MPK)", 'sync.cost.center.cost', 'sync_cost_center_lines'),
                ("payroll sync", 'sync.employee.cost', 'sync_salary_lines')):
            log("ANALYTICS  %s from %s" % (title, SYNC_DATE_FROM))
            log("-" * 58)
            try:
                with self.env.cr.savepoint():
                    result = getattr(self.env[service].sudo(), method)(
                        company_id=SYNC_COMPANY_ID, date_from=SYNC_DATE_FROM)
            except UserError as error:
                log("SKIPPED: %s" % error)
                log("Run the wizard by hand once Enterprise is reachable.")
                log("")
                continue
            log("found %(records_found)s, created %(created)s, updated %(updated)s, "
                "skipped %(skipped)s, failed %(failed)s" % result)
            for detail in result.get('details', []):
                if not detail.startswith(("Skipped source ID", "Pominięto identyfikator")):
                    log("   %s" % detail)
            log("")

        log("ANALYTICS  manual payroll documents now duplicated by the sync")
        log("-" * 58)
        self._step_payroll_duplicates(log)
        log("")

        log("ANALYTICS  checks")
        log("-" * 58)
        self._step_analytics_checks(log)

    def _step_missing_rcp_weeks(self, log):
        """Create the RCP documents a week split by a month end is missing.

        A week across a month boundary needs one document per month part
        (hr.rcp._get_week_rcp_dates); week 36 of 2026 had only the August
        one, so the hours of 1-6 September were never costed. Only the empty
        document is created - computing and posting it is the normal RCP
        routine, done like any other week. Coverage is checked on the
        week's monday and the month, not on the document date: some
        documents are dated on a Tuesday.
        """
        Rcp = self.env['hr.rcp'].sudo()
        today = fields.Date.context_today(self)
        companies = Rcp.search([('date', '>=', SYNC_DATE_FROM)]).mapped('company_id')
        created = 0
        for company in companies:
            docs = Rcp.search([('company_id', '=', company.id),
                               ('request_monday_date', '>=', SYNC_DATE_FROM)])
            covered = {(d.request_monday_date, d.date.month) for d in docs
                       if d.request_monday_date and d.date}
            monday = min(d.request_monday_date for d in docs) if docs else None
            while monday and monday + relativedelta(days=6) < today:
                for rcp_date in Rcp._get_week_rcp_dates(monday):
                    if (monday, rcp_date.month) in covered:
                        continue
                    record = Rcp.with_company(company).create(
                        {'date': rcp_date, 'company_id': company.id})
                    created += 1
                    log("   %s: created %s (week of %s)"
                        % (company.display_name, record.display_name, monday))
                monday += relativedelta(weeks=1)
        log("created: %d%s" % (created, " - compute and post them as usual"
                                if created else ""))

    def _step_project_codes(self, log):
        """Make every project's number and its account's code agree.

        1. A customer reference typed into the number ("26-1069 CEDO/...")
           is split off into Client reference - the allocator does not
           recognise such a number and hands it out again.
        2. A project account coded with another number, or with free text,
           gets its project's number. Accounts coded after a cost centre
           (A011-1, A062-3) are the cost category projects and stay.
        3. A project sharing its account with another one is moved to the
           unlinked account that carries its own number, where its own
           timesheets were booked.
        Anything that cannot be decided this way is listed and left.
        """
        Project = self.env['project.project'].sudo().with_context(
            active_test=False, skip_project_freeze_guard=True)
        Account = self.env['account.analytic.account'].sudo().with_context(
            active_test=False)
        number_re = r'^\d{2}-\d{4,}$'
        changed = 0

        for project in Project.search([('code', '!=', False)]):
            if re.match(number_re, project.code):
                continue
            match = re.match(r'^(\d{2}-\d{4,})[\s,;:/-]*(.*)$', project.code.strip())
            number = match and match.group(1)
            if not number or Project.search_count([
                    ('code', '=', number), ('company_id', '=', project.company_id.id),
                    ('id', '!=', project.id)]):
                log("   LEFT FOR REVIEW project %s: code %r is not a free project "
                    "number" % (project.id, project.code))
                continue
            vals = {'code': number}
            reference = match.group(2).strip()
            if reference and not project.client_reference:
                vals['client_reference'] = reference
            log("   project %s: code %r -> %s%s" % (
                project.id, project.code, number,
                (", client reference %r" % reference) if 'client_reference' in vals else ''))
            project.write(vals)
            changed += 1

        self.env.flush_all()
        self.env.cr.execute("""
            SELECT analytic_account_id FROM project_project
             WHERE analytic_account_id IS NOT NULL
             GROUP BY 1 HAVING count(*) > 1""")
        shared = {row[0] for row in self.env.cr.fetchall()}

        # Repeated: freeing a number on one account can unblock another.
        for _round in range(3):
            moved = 0
            for project in Project.search([('code', '!=', False),
                                           ('analytic_account_id', '!=', False)]):
                account = project.analytic_account_id
                if account.id in shared or account.code == project.code:
                    continue
                if account.code and re.match(r'^[Aa]\d{3}', account.code):
                    continue
                if Account.search_count([('code', '=', project.code),
                                         ('id', '!=', account.id)]):
                    continue
                log("   account %s of project %s: code %r -> %s"
                    % (account.id, project.id, account.code, project.code))
                account.code = project.code
                moved += 1
            changed += moved
            if not moved:
                break

        for account_id in shared:
            for project in Project.search([('analytic_account_id', '=', account_id)]):
                own = Account.search([('code', '=', project.code),
                                      ('id', '!=', account_id)])
                own = own.filtered(lambda a: not Project.search_count(
                    [('analytic_account_id', '=', a.id)]))
                if len(own) != 1:
                    continue
                log("   project %s: moved from shared account %s to its own "
                    "account %s (%s)" % (project.id, account_id, own.id, own.code))
                project.write({'analytic_account_id': own.id})
                changed += 1
        self.env.flush_all()
        self.env.cr.execute("""
            SELECT analytic_account_id FROM project_project
             WHERE analytic_account_id IS NOT NULL
             GROUP BY 1 HAVING count(*) > 1""")
        for (account_id,) in self.env.cr.fetchall():
            log("   LEFT FOR REVIEW account %s is still shared by several projects"
                % account_id)
        log("changed: %d" % changed)

    def _step_project_account_codes(self, log):
        """Give a project's own account the project number when it wears a
        cost centre code another active account also carries.

        A duplicated code is dropped by the cost centre sync, so every
        Enterprise cost for it is skipped (A056: 273 443 zł in 2026). The
        project account is the one in the wrong: projects are identified
        by their number, the cost centre by its code. Only duplicated
        codes are touched, only accounts linked to exactly one project
        that has a number.
        """
        Account = self.env['account.analytic.account'].sudo()
        Project = self.env['project.project'].sudo().with_context(active_test=False)
        self.env.flush_all()
        self.env.cr.execute("""
            SELECT upper(trim(code)) FROM account_analytic_account
             WHERE active AND code ~ '^ *[Aa][0-9]{3} *$'
             GROUP BY 1 HAVING count(*) > 1""")
        duplicated = [row[0] for row in self.env.cr.fetchall()]
        if not duplicated:
            log("none.")
            return
        for code in duplicated:
            accounts = Account.search([('code', '=ilike', code), ('active', '=', True)])
            for account in accounts:
                projects = Project.search([('analytic_account_id', '=', account.id)])
                if len(projects) != 1 or not projects.code:
                    continue
                if Account.with_context(active_test=False).search_count(
                        [('code', '=', projects.code), ('id', '!=', account.id)]):
                    log("   LEFT FOR REVIEW %s: project %s number %s is already "
                        "used by another account" % (code, projects.id, projects.code))
                    continue
                log("   %s: account %s of project %s recoded to %s"
                    % (code, account.id, projects.display_name, projects.code))
                account.code = projects.code
            left = Account.search_count([('code', '=ilike', code), ('active', '=', True)])
            if left > 1:
                log("   LEFT FOR REVIEW %s: still %d active accounts - none of the "
                    "rest is a numbered project's own account" % (code, left))

    def _step_person_accounts(self, log):
        """Create the per-person cost centre accounts Enterprise already uses.

        Codes are identical in Enterprise and CE by rule, so an Enterprise
        code with no CE account means costs that are skipped. Only codes
        that follow the standard - Axxx-E-<an existing E-account code> with
        an active Axxx root - are created, named the way the migration
        names them; anything else is listed for a person to decide.
        """
        report = self.env['sync.cost.center.cost'].sudo().get_mapping_report(
            company_id=SYNC_COMPANY_ID, date_from=SYNC_DATE_FROM)
        missing = sorted({row['code'] for row in report['unmatched']
                          if row.get('reason') == 'missing' and row.get('code')})
        if not missing:
            log("none.")
            return
        Account = self.env['account.analytic.account'].sudo().with_context(
            active_test=False)
        Employee = self.env['hr.employee'].sudo().with_context(active_test=False)
        created = 0
        for code in missing:
            match = re.match(r'^(A\d{3})-(E-.+)$', code)
            root = match and Account.search(
                [('code', '=', match.group(1)), ('active', '=', True)])
            e_account = match and Account.search([('code', '=', match.group(2))])
            employee = e_account and len(e_account) == 1 and Employee.search(
                [('account_id', '=', e_account.id)], limit=1)
            if not (match and len(root) == 1 and employee):
                log("   LEFT FOR REVIEW %s: not Axxx-E-<E-account> with one "
                    "active root and a known employee" % code)
                continue
            label = root.with_context(lang='pl_PL').name or root.code
            account = Account.create({
                'name': '%s - %s' % (label, employee.name),
                'code': code,
                'plan_id': root.plan_id.id,
                'company_id': root.company_id.id,
            })
            for lang in ('en_US', 'pl_PL'):
                account.with_context(lang=lang).name = '%s - %s' % (label, employee.name)
            # Account.create() hangs a new account under "Projects".
            account.parent_id = False
            created += 1
            log("   created %s  %s" % (code, account.name))
        log("created: %d" % created)

    def _step_payroll_duplicates(self, log):
        """Reverse posted payroll transfer documents the sync now covers.

        Payroll Enterprise labelled without an E-code was never imported,
        so it was keyed in by hand (ATD documents, January-March 2026 on
        the development database). Matched by account code it now arrives
        from Enterprise as well. Only the lines sitting on an E-account in
        a month that account already has synchronised payroll for are
        reversed; the rest stay, as the only record of that salary.
        """
        Document = self.env['analytic.transfer.document'].sudo()
        documents = Document.search([
            ('state', '=', 'posted'), ('posting_origin', '=', 'payroll'),
            ('reversal_of_id', '=', False),
        ]).filtered(lambda d: not d.reversal_ids.filtered(
            lambda r: r.state != 'cancelled'))
        if not documents:
            log("no manual payroll documents.")
            return

        self.env.flush_all()
        self.env.cr.execute("""
            SELECT DISTINCT l.account_id, date_trunc('month', l.date)::date
              FROM account_analytic_line l
              JOIN sync_mapping m ON m.target_id = l.id AND m.model_name = %s
             WHERE l.posting_origin = 'payroll'
        """, (SALARY_SYNC_KEY,))
        synced = set(self.env.cr.fetchall())

        reversed_count = 0
        for document in documents:
            month = document.date.replace(day=1)
            covered = document.line_ids.filtered(
                lambda line: not line.destination_account_id
                and line.source_account_id.id in self._person_account_ids()
                and (line.source_account_id.id, month) in synced)
            # A line with no synchronised payroll behind it is the only
            # record of that salary (it is not in Enterprise under the
            # person's code) - reversing it would wipe the salary out.
            kept = document.line_ids - covered
            if not covered:
                log("   %s (%s): nothing duplicated - left as it is"
                    % (document.name, document.date))
                continue
            document._create_reversal(covered)
            reversed_count += 1
            log("   reversed %s (%s): %d of %d line(s), %.2f"
                % (document.name, document.date, len(covered),
                   len(document.line_ids), sum(covered.mapped('amount'))))
            for line in kept:
                log("      KEPT (only record of this payroll): %s %.2f"
                    % (line.source_account_id.code or line.source_account_id.name,
                       line.amount))
        log("reversed: %d" % reversed_count)

    def _step_analytics_checks(self, log):
        """What the account standard promises, checked on the data."""
        self.env.flush_all()
        cr = self.env.cr
        problems = 0
        checks = (
            ("duplicated active cost centre codes (their Enterprise costs are skipped)",
             """SELECT upper(trim(code)) FROM account_analytic_account
                 WHERE active AND code LIKE 'A%%'
                 GROUP BY 1 HAVING count(*) > 1"""),
            ("payroll on per-person cost centre accounts (must be on the E-account)",
             """SELECT a.code FROM account_analytic_line l
                  JOIN account_analytic_account a ON a.id = l.account_id
                 WHERE a.code ~ '^A[0-9]{3}-E-' AND l.document_no LIKE 'WYN/%%'
                   AND NOT EXISTS (SELECT 1 FROM hr_employee e WHERE e.account_id = a.id)
                 GROUP BY 1"""),
            ("employee accounts not coded E-... or Axxx-E-...",
             """SELECT a.code FROM hr_employee e
                  JOIN account_analytic_account a ON a.id = e.account_id
                 WHERE a.code !~ '^(A[0-9]{3}-)?E-'"""),
            ("sales people still with an E-account or an offer project account",
             """SELECT f.code FROM account_analytic_account f
                 WHERE f.code LIKE ANY(%(merged)s)
                   AND (EXISTS (SELECT 1 FROM account_analytic_account e
                                 WHERE e.code = substring(f.code from 6))
                        OR NOT EXISTS (SELECT 1 FROM hr_employee h
                                        WHERE h.account_id = f.id))"""),
            ("archived E-accounts of employees still working here",
             # Not left: no past departure date and, where contracts exist,
             # one still running - HR often leaves people who left active.
             """SELECT a.code FROM hr_employee e
                  JOIN account_analytic_account a ON a.id = e.account_id
                 WHERE e.active AND NOT a.active
                   AND (e.departure_date IS NULL OR e.departure_date >= CURRENT_DATE)
                   AND (NOT EXISTS (SELECT 1 FROM hr_contract c
                                     WHERE c.employee_id = e.id AND c.state <> 'cancel')
                        OR EXISTS (SELECT 1 FROM hr_contract c
                                    WHERE c.employee_id = e.id AND c.state <> 'cancel'
                                      AND (c.date_end IS NULL OR c.date_end >= CURRENT_DATE)))"""),
        )
        for title, query in checks:
            cr.execute(query, {'merged': ['%s-E-%%' % code for code in MERGED_COST_CENTRES]})
            found = [row[0] for row in cr.fetchall()]
            if found:
                problems += 1
                log("   %s: %d - %s" % (title, len(found), ', '.join(found[:15])))
            else:
                log("   %s: none" % title)

        cr.execute("""
            SELECT to_char(date, 'YYYY-MM'), count(DISTINCT account_id), sum(amount)
              FROM account_analytic_line
             WHERE posting_origin = 'payroll' AND date >= %s
             GROUP BY 1 ORDER BY 1""", (SYNC_DATE_FROM,))
        log("")
        log("payroll on E-accounts by month (people, amount):")
        for month, people, amount in cr.fetchall():
            log("   %s  %4d  %14.2f" % (month, people, amount))
        log("")
        log("ANALYTICS %s" % ("CLEAN" if not problems
                               else "- %d check(s) above need a look" % problems))

    # ------------------------------------------------------------------
    # the whole thing, in one click
    # ------------------------------------------------------------------
    def _affected_codes(self):
        """Projects that actually have a wage sitting on an employee account.

        The repair walks these, not all fifty. Iterating every
        non-absorbing project would recompute every RCP week any of them
        appears in - 182 documents on the development database - to fix
        the one or two that are wrong.
        """
        accounts = self._unproductive_projects().mapped('analytic_account_id')
        rows = self._live_wage_rows(accounts)
        # Summed per project and employee account, not merely present: a
        # zero-rate posting, or one and its correction, leaves nothing on
        # the E account, and walking such a project rebuilds its overheads
        # for nothing - and stops the whole run when one of them cannot be
        # rebuilt (a position without overheads, like a manager's).
        held = {}
        for row in rows:
            e_side, p_side, effect = self._employee_effect(row)
            if p_side.code:
                key = (p_side.code, e_side.id)
                held[key] = held.get(key, 0.0) + (effect or 0.0)
        return tuple(sorted({code for (code, _e), amount in held.items()
                             if abs(amount) > 0.005}))

    def _run_auto(self, log, write):
        """Every step, in order, deciding its own scope as it goes.

        Repricing is deliberately not part of this. It deletes sheet
        lines and rebuilds them, and the lines that qualify run to over
        two million złoty of 2023-2024 history - that is a decision
        somebody takes on purpose, not a side effect of pressing Start.
        The run reports how many there are and stops there.
        """
        log("STEP 0/6  preflight")
        log("-" * 58)
        # The count comes back rather than being stashed on the record:
        # a recordset attribute is read-only, and a field would invite
        # somebody to trust a stale value from an earlier run.
        if self._step_preflight(log):
            log("")
            log("STOPPED at preflight. Fix the blockers above and start "
                "again - nothing was touched.")
            return
        log("")

        log("STEP 1/6  audit")
        log("-" * 58)
        before = self._step_audit(log, brief=True)
        codes = self._affected_codes()
        log("")
        if not codes and abs(before) <= 0.005:
            log("Nothing to repair - no non-absorbing project charges an "
                "employee account.")
            return
        log("projects to repair: %s" % (', '.join(codes) or '(none)'))
        log("")

        log("STEP 2/6  line statuses")
        log("-" * 58)
        self._step_status(log)
        log("")

        log("STEP 3/6  reverse the wage postings")
        log("-" * 58)
        for code in codes:
            log("-- %s" % code)
            self.with_context(mj_rcp_scope=(code,))._step_reverse(log)
        log("")

        log("STEP 4/6  rows with no RCP line")
        log("-" * 58)
        self._step_mirror(log)
        log("")

        log("STEP 5/6  rebuild the overheads")
        log("-" * 58)
        for code in codes:
            log("-- %s" % code)
            self.with_context(mj_rcp_scope=(code,))._step_overheads(log)
        log("")

        log("STEP 6/6  verify")
        log("-" * 58)
        self._step_verify(log, brief=True)

        # Not done, on purpose - but not hidden either.
        _pending, mispriced = self._mispriced_pending()
        if mispriced:
            log("")
            log("NOT DONE HERE: %d sheet line(s) are priced at the "
                "productive rate and will be refused on posting."
                % len(mispriced))
            log("Repricing deletes and rebuilds them. Use step 7 by hand, "
                "one project at a time, once somebody has agreed the scope.")

    def action_start(self):
        """Start the repair. One button, no configuration."""
        return self._execute(auto=True, write=True)

    def action_preview(self):
        """The same run, with nothing kept."""
        return self._execute(auto=True, write=False)

    # ------------------------------------------------------------------
    # the buttons
    # ------------------------------------------------------------------
    def _execute(self, auto, write):
        """Run something and show what it did.

        The savepoint is what makes "read it first" real: without
        `write` it is rolled back and the report is all that survives. A
        step that refuses - a wage posted, a rate still wrong - raises,
        which unwinds the same way, and the report still reaches the
        screen because the wizard row was written before the savepoint
        was taken.
        """
        self.ensure_one()
        entries = []
        log = entries.append

        # An automatic run picks its own companies. hr_rcp scopes its
        # overhead lookups by the active company, so a single-company
        # run would quietly leave the other companies' overheads behind.
        if auto:
            companies = self._unproductive_projects().mapped('company_id')
            companies = companies or self.company_id
        else:
            companies = self.company_id

        log("mode: %s" % ("REPAIRING" if write else "PREVIEW - nothing kept"))
        log("companies: %s"
            % ', '.join(c.display_name for c in companies))
        if not auto:
            log("scope: %s" % ((self.mpk or '').strip()
                               or "all non-absorbing projects"))
        log("=" * 58)

        try:
            with self.env.cr.savepoint():
                for company in companies:
                    wizard = self.with_company(company)
                    if len(companies) > 1:
                        log("")
                        log("#" * 58)
                        log("# %s" % company.display_name)
                        log("#" * 58)
                    if auto:
                        wizard._run_auto(log, write)
                    else:
                        getattr(wizard, '_step_%s' % self.step)(log)
                if auto:
                    # Once, not per company: these steps select on the
                    # ledger itself, and the one that posts switches to
                    # each line's company on its own.
                    log("")
                    log("#" * 58)
                    log("# ledger integrity")
                    log("#" * 58)
                    self._run_ledger(log)
                    log("")
                    log("#" * 58)
                    log("# analytics: Enterprise syncs and payroll")
                    log("#" * 58)
                    self._run_analytics(log)
                if not write:
                    raise _DryRun()
        except _DryRun:
            log("")
            log("NOTHING WAS KEPT.")
        except UserError as error:
            # Reported rather than raised: a dialog would replace the
            # log with a one-line message, and the log is the reason to
            # run this at all. The savepoint has already unwound.
            log("")
            log("STOPPED: %s" % error)
            log("NOTHING WAS KEPT.")

        self.env.invalidate_all()
        record = self.browse(self.id)
        record.output = '\n'.join(entries)
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': record.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_run(self):
        """Run the single step chosen by hand."""
        return self._execute(auto=False, write=self.apply_changes)
