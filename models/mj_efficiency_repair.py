# -*- coding: utf-8 -*-
"""Data repair behind the Workforce Efficiency board (November 2026).

Each step was first run by hand on db_dev; production has no shell, so the
same logic lives here behind Preview / Repair buttons. Preview runs every
step inside a savepoint and rolls it back - the report is all that is kept.

1. Double reversal: a repair correction ("Korekta: wycofanie absorpcji
   robocizny") of a posting that RCP itself reversed later. The posting was
   then undone twice; the correction pair is removed.
2. Manual absence booked on a day an approved leave already covers. The
   leave is the record of truth; the manual line is removed.
3. Approved (active) leaves without timesheet lines - mostly validated in
   2025, before lines were generated. The lines are created for days in the
   period that carry no other timesheet.
4. Timesheet lines detached from an approved sheet (fixed for the future in
   hr_extension 17.0.0.0.12). They are put back on their sheet, and every
   posted RCP week they belong to is unlocked, recomputed, posted and
   locked again, so the hours reach the ledger as correction lines.
"""

from datetime import date

from odoo import _, fields, models
from odoo.exceptions import UserError

CORRECTION_MARKER = 'Korekta: wycofanie absorpcji robocizny'


class _Rollback(Exception):
    """Unwinds the preview savepoint once the report is written."""


class MjEfficiencyRepair(models.TransientModel):
    _name = 'mj.efficiency.repair'
    _description = 'Workforce Efficiency data repair (one-off)'

    date_from = fields.Date(string='From', required=True,
                            default=lambda self: date(fields.Date.today().year, 1, 1))
    date_to = fields.Date(string='To', required=True, default=fields.Date.context_today)
    company_id = fields.Many2one('res.company', string='Company', required=True,
                                 default=lambda self: self.env.company)
    output = fields.Text(string='Report', readonly=True)

    # ------------------------------------------------------------------
    # buttons
    # ------------------------------------------------------------------
    def action_preview(self):
        return self._execute(write=False)

    def action_repair(self):
        return self._execute(write=True)

    def _execute(self, write):
        self.ensure_one()
        if not self.env.user.has_group('base.group_system'):
            raise UserError(_('Only an administrator can run this repair.'))
        if self.date_from > self.date_to:
            raise UserError(_('The period starts after it ends.'))
        lines = []
        log = lines.append
        log(_('MODE: %s', _('REPAIR') if write else _('PREVIEW - nothing is kept')))
        log(_('Company: %(company)s, period %(start)s - %(end)s',
              company=self.company_id.name, start=self.date_from, end=self.date_to))
        this = self.with_company(self.company_id)
        try:
            with self.env.cr.savepoint():
                this._step_double_reversals(log)
                this._step_duplicate_absence(log)
                this._step_missing_leave_lines(log)
                this._step_detached_lines(log)
                if not write:
                    raise _Rollback()
        except _Rollback:
            pass
        self.output = '\n'.join(str(line) for line in lines)
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    # ------------------------------------------------------------------
    # 1. double reversal
    # ------------------------------------------------------------------
    def _step_double_reversals(self, log):
        log('')
        log(_('1. Corrections of postings RCP reversed itself'))
        cr = self.env.cr
        cr.execute("""
            SELECT c.id, c.amount, c.rcp_reverse_of_id
              FROM account_analytic_line c
              JOIN account_analytic_line o ON o.id = c.rcp_reverse_of_id
             WHERE c.name LIKE %s
               AND c.company_id = %s
               AND COALESCE(c.is_reversed, FALSE) = FALSE
               AND o.is_reversed = TRUE
               AND EXISTS (SELECT 1 FROM account_analytic_line s
                            WHERE s.rcp_reverse_of_id = o.id
                              AND s.id <> c.id
                              AND s.rcp_move_key LIKE %s)
        """, (CORRECTION_MARKER + '%', self.company_id.id, '%:REVERSE:%'))
        rows = cr.fetchall()
        AAL = self.env['account.analytic.line'].sudo()
        doubles = AAL.browse([row[0] for row in rows])
        doubles |= doubles.mapped('transfer_pair_id')
        for line in doubles.filtered(lambda l: l.amount < 0):
            log(_('   %(date)s %(account)s %(amount).2f (%(employee)s), original %(original)s',
                  date=line.date, account=line.account_id.code or '',
                  amount=line.amount, employee=line.employee_id.name or '-',
                  original=line.rcp_reverse_of_id.id))
        if doubles:
            doubles.with_context(skip_check_state=True).unlink()
        log(_('   removed: %s lines', len(doubles)))

    # ------------------------------------------------------------------
    # 2. manual absence on a day a leave covers
    # ------------------------------------------------------------------
    def _internal_projects(self):
        return self.env['res.company'].sudo().search([]).internal_project_id

    def _step_duplicate_absence(self, log):
        log('')
        log(_('2. Manual absence on days an approved leave already covers'))
        internal = self._internal_projects()
        if not internal:
            log(_('   no internal (absence) project configured - skipped'))
            return
        cr = self.env.cr
        # Only a positive manual absence (a task classed as absence) repeats
        # the leave. A negative one is somebody cancelling the leave by hand
        # because the person worked that day after all - that is for HR,
        # not for this repair, and removing it would add the absence back.
        cr.execute("""
            SELECT m.id, m.unit_amount
              FROM account_analytic_line m
              JOIN project_task t ON t.id = m.task_id
             WHERE m.company_id = %s
               AND m.date BETWEEN %s AND %s
               AND m.project_id IN %s
               AND m.holiday_id IS NULL
               AND m.global_leave_id IS NULL
               AND m.unit_amount <> 0
               AND t.mj_absence_class IS NOT NULL
               AND t.mj_absence_class <> 'not_absence'
               AND COALESCE(m.rcp_checked, FALSE) = FALSE
               AND EXISTS (SELECT 1 FROM account_analytic_line w
                             JOIN hr_leave l ON l.id = w.holiday_id AND l.active
                            WHERE w.employee_id = m.employee_id
                              AND w.date = m.date
                              AND w.unit_amount <> 0)
        """, (self.company_id.id, self.date_from, self.date_to, tuple(internal.ids)))
        rows = cr.fetchall()
        AAL = self.env['account.analytic.line'].sudo()
        lines = AAL.browse([r[0] for r in rows if r[1] > 0])
        for negative in AAL.browse([r[0] for r in rows if r[1] < 0]):
            log(_('   left for HR - leave cancelled by hand, approved request still active: '
                  '%(employee)s %(date)s %(hours).1f h',
                  employee=negative.employee_id.name, date=negative.date,
                  hours=negative.unit_amount))
        by_employee = {}
        for line in lines:
            by_employee.setdefault(line.employee_id.name or '-', 0.0)
            by_employee[line.employee_id.name or '-'] += line.unit_amount
        for name, hours in sorted(by_employee.items()):
            log(_('   %(name)s: %(hours).1f h', name=name, hours=hours))
        hours = sum(lines.mapped('unit_amount'))
        if lines:
            lines.with_context(skip_check_state=True).unlink()
        log(_('   removed: %(count)s lines, %(hours).1f h', count=len(lines), hours=hours))

    # ------------------------------------------------------------------
    # 3. approved leaves without lines
    # ------------------------------------------------------------------
    def _step_missing_leave_lines(self, log):
        log('')
        log(_('3. Approved leaves without timesheet lines'))
        Leave = self.env['hr.leave'].sudo()
        leaves = Leave.search([
            ('state', '=', 'validate'),
            ('employee_id.company_id', '=', self.company_id.id),
            ('request_date_to', '>=', self.date_from),
            ('request_date_from', '<=', self.date_to),
            ('timesheet_ids', '=', False),
        ])
        AAL = self.env['account.analytic.line'].sudo()
        cr = self.env.cr
        created = AAL.browse()
        skipped = []
        for leave in leaves.sorted('request_date_from'):
            employee = leave.employee_id
            if not employee.active:
                skipped.append(_('%s (employee archived)', employee.name))
                continue
            if not leave.holiday_status_id.timesheet_generate:
                continue
            leave_type = leave.holiday_status_id
            if leave_type.company_id:
                project, task = leave_type.timesheet_project_id, leave_type.timesheet_task_id
            else:
                project = employee.company_id.internal_project_id
                task = employee.company_id.leave_timesheet_task_id
            if not project or not task:
                skipped.append(_('%s (no absence project/task)', employee.name))
                continue
            # The leave itself is a calendar leave: left in, it zeroes its
            # own days. Core does the same when it generates the lines.
            own = self.env['resource.calendar.leaves'].sudo().search(
                [('holiday_id', '=', leave.id)])
            work = employee.list_work_time_per_day(
                leave.date_from, leave.date_to,
                domain=[('id', 'not in', own.ids)] if own else None)
            vals = []
            for index, (day, hours) in enumerate(work):
                if not (self.date_from <= day <= self.date_to):
                    continue
                cr.execute("""SELECT 1 FROM account_analytic_line
                               WHERE employee_id = %s AND date = %s
                                 AND project_id IS NOT NULL AND unit_amount <> 0
                               LIMIT 1""", (employee.id, day))
                if cr.fetchone():
                    continue
                vals.append(leave._timesheet_prepare_line_values(
                    index, work, day, hours, project, task))
            if not vals:
                continue
            try:
                with cr.savepoint():
                    created |= AAL.with_context(skip_check_state=True).create(vals)
            except Exception as error:  # reported, the other leaves go on
                skipped.append('%s: %s' % (employee.name, str(error).splitlines()[0][:100]))
        by_employee = {}
        for line in created:
            by_employee.setdefault(line.employee_id.name, 0.0)
            by_employee[line.employee_id.name] += line.unit_amount
        for name, hours in sorted(by_employee.items()):
            log(_('   %(name)s: %(hours).1f h', name=name, hours=hours))
        for reason in skipped:
            log(_('   skipped: %s', reason))
        log(_('   created: %(count)s lines, %(hours).1f h',
              count=len(created), hours=sum(created.mapped('unit_amount'))))

    # ------------------------------------------------------------------
    # 4. lines detached from an approved sheet, and their RCP
    # ------------------------------------------------------------------
    def _step_detached_lines(self, log):
        log('')
        log(_('4. Timesheet lines detached from an approved sheet'))
        internal = self._internal_projects()
        cr = self.env.cr
        cr.execute("""
            SELECT a.id, s.id
              FROM account_analytic_line a
              JOIN hr_timesheet_sheet s
                ON s.employee_id = a.employee_id
               AND a.date BETWEEN s.date_start AND s.date_end
               AND s.company_id = a.company_id
               AND s.state = 'done'
             WHERE a.company_id = %s
               AND a.date BETWEEN %s AND %s
               AND a.project_id IS NOT NULL
               AND a.sheet_id IS NULL
               AND a.holiday_id IS NULL
               AND a.global_leave_id IS NULL
               AND a.unit_amount <> 0
               AND NOT (a.project_id = ANY(%s))
        """, (self.company_id.id, self.date_from, self.date_to, internal.ids or [0]))
        pairs = cr.fetchall()
        AAL = self.env['account.analytic.line'].sudo()
        by_sheet = {}
        for line_id, sheet_id in pairs:
            by_sheet.setdefault(sheet_id, []).append(line_id)
        attached = AAL.browse()
        for sheet_id, ids in by_sheet.items():
            group = AAL.browse(ids)
            frozen = group.filtered(
                lambda l: l.project_id._is_project_execution_frozen_for_controls())
            for line in frozen:
                log(_('   skipped (project settled): %(employee)s %(date)s %(hours).1f h %(project)s',
                      employee=line.employee_id.name, date=line.date,
                      hours=line.unit_amount, project=line.project_id.display_name))
            group -= frozen
            if not group:
                continue
            try:
                with cr.savepoint():
                    group.with_context(skip_check_state=True).write({'sheet_id': sheet_id})
                attached |= group
            except Exception as error:
                log(_('   refused: %(count)s lines of %(employee)s: %(error)s',
                      count=len(group), employee=group[0].employee_id.name,
                      error=str(error).splitlines()[0][:120]))
        log(_('   re-attached: %(count)s lines, %(hours).1f h',
              count=len(attached), hours=sum(attached.mapped('unit_amount'))))
        if attached:
            self._post_reattached_hours(attached, log)

    def _post_reattached_hours(self, attached, log):
        """Unlock, recompute, post and lock every posted RCP week that now
        has hours to take. Posted synchronously: the queue runner would see
        the weeks only after this transaction commits, locked again."""
        Rcp = self.env['hr.rcp']
        docs = Rcp.search([
            ('company_id', '=', self.company_id.id),
            ('stage', '=', 'posted'),
        ])
        Param = self.env['ir.config_parameter'].sudo()
        queue_flag = Param.get_param('hr_rcp.use_analytic_queue', '1')
        Param.set_param('hr_rcp.use_analytic_queue', '0')
        total_lines, total_value, errors = 0, 0.0, 0
        try:
            for doc in docs.sorted('date'):
                eligible = self.env['account.analytic.line'].sudo().search_count(
                    doc._get_analytic_domain() + [('id', 'in', attached.ids)])
                if not eligible:
                    continue
                was_locked = doc.locked
                before = set(doc.rcp_lines.ids)
                if was_locked:
                    doc.action_unlock()
                doc.action_get_analytic_lines()
                if doc.rcp_lines.filtered(lambda l: l.status == 'new'):
                    doc.action_post_to_analytic()
                new = doc.rcp_lines.filtered(lambda l: l.id not in before)
                value = sum(l.quantity * l.unit_cost for l in new)
                failed = len(new.filtered(lambda l: l.status == 'error'))
                if was_locked:
                    doc.action_lock()
                total_lines += len(new)
                total_value += value
                errors += failed
                log(_('   %(week)s: %(count)s new lines, %(hours).1f h, %(value).2f zł%(errors)s',
                      week=doc.name, count=len(new), hours=sum(new.mapped('quantity')),
                      value=value, errors=(_(', ERRORS: %s', failed) if failed else '')))
        finally:
            Param.set_param('hr_rcp.use_analytic_queue', queue_flag)
        log(_('   RCP: %(count)s correction lines posted, %(value).2f zł, errors: %(errors)s',
              count=total_lines, value=total_value, errors=errors))
