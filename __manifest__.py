{
    'name': 'MJ RCP Repair (temporary)',
    'version': '17.0.1.16.0',
    'author': 'DSquare Net',
    'company': 'MJ Group Sp. z o.o.',
    'license': 'OPL-1',
    'summary': 'One-off repair: wages absorbed by non-absorbing projects',
    'description': """
MJ RCP Repair
=============
A disposable module. Install it, run the repair from
Settings > Technical > MJ RCP Repair, then uninstall it.

It exists because the repair has to run inside Odoo - on a Docker
deployment there is no shell to pipe a script into - and because the
work it does is a one-off: idle time and autocorrections put wages on
employee analytic accounts before the posting guard existed, and those
postings have to come off.

Nothing here is meant to stay installed. The model is transient, the
menu sits under Technical, and uninstalling leaves only the corrections
themselves behind - which is the point.
    """,
    'category': 'Accounting/Analytic',
    'depends': [
        'hr_rcp',
        'sync_hub',
        'analytic_cost_control',
        'hr_extension',
        # The guard this repair assumes (`mj_is_idle_time` as a second
        # signal for a project that must not absorb) lives here, and so
        # does the sheet-against-ledger view used to check the result.
        'mj_finance_core',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/mj_rcp_repair_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
