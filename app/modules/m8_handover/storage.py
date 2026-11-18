"""交接单持久化。

设计要点：
- handovers 表只追加：每个业务日的交接单以 sheet_no 形成版本链，
  上一班已签收（signed）的归档永不改写；
- 重判定只产生新 sheet_no，旧行保留，页面通过 redecided 列出变化时段；
- 同一批结果重跑（同 run_id）幂等，不新增风险段、不新增交接单；
- 补发在事务内完成，任何一步失败整体回滚，旧版交接单保持有效。
"""
import json
from app.errors import DomainError
from app.store import connection
from app.modules.m8_handover.rules import diff_periods

SIGNED = '已签收'
DRAFT = '待签收'
SUPERSEDED = '已替换'


def _row_to_sheet(row):
    sheet = json.loads(row['body'])
    sheet.update(sheet_no=row['sheet_no'], run_id=row['run_id'],
                 business_day=row['business_day'], state=row['state'])
    return sheet


def list_sheets(business_day=None):
    with connection() as db:
        if business_day is None:
            rows = db.execute('SELECT * FROM handovers ORDER BY rowid DESC LIMIT 100').fetchall()
        else:
            rows = db.execute(
                'SELECT * FROM handovers WHERE business_day=? ORDER BY sheet_no',
                (business_day,)).fetchall()
    return [_row_to_sheet(r) for r in rows]


def get_sheet(business_day, sheet_no):
    with connection() as db:
        row = db.execute(
            'SELECT * FROM handovers WHERE business_day=? AND sheet_no=?',
            (business_day, sheet_no)).fetchone()
    if not row:
        raise DomainError('NOT_FOUND', '交接单不存在', 404)
    return _row_to_sheet(row)


def latest_sheet(business_day):
    with connection() as db:
        row = db.execute(
            'SELECT * FROM handovers WHERE business_day=? ORDER BY sheet_no DESC LIMIT 1',
            (business_day,)).fetchall()
    if not row:
        return None
    return _row_to_sheet(row[0])


def issue(business_day, run_id, handover, redecided=None):
    """补发一张新交接单。

    幂等：同一 run_id 已补发过则直接返回旧单，重跑不新增风险段。
    已签收旧单保持原样，仅状态流转为“已替换”；补发失败时事务回滚，
    旧版交接单（含已签收归档）完整保留。
    """
    if not isinstance(business_day, str) or not business_day:
        raise DomainError('BUSINESS_DAY', '业务日不能为空')
    db = connection()
    try:
        db.execute('BEGIN IMMEDIATE')
        existing = db.execute(
            'SELECT * FROM handovers WHERE business_day=? AND run_id=?',
            (business_day, run_id)).fetchall()
        if existing:
            db.rollback()
            return _row_to_sheet(existing[0])
        prior_row = db.execute(
            'SELECT * FROM handovers WHERE business_day=? ORDER BY sheet_no DESC LIMIT 1',
            (business_day,)).fetchall()
        prior = _row_to_sheet(prior_row[0]) if prior_row else None
        sheet_no = (prior_row[0]['sheet_no'] + 1) if prior_row else 1
        if prior and prior['state'] == SIGNED:
            db.execute('UPDATE handovers SET state=? WHERE business_day=? AND sheet_no=?',
                       (SUPERSEDED, business_day, prior['sheet_no']))
        if redecided is None:
            redecided = diff_periods(prior['periods'], handover['periods']) if prior else []
        sheet = {
            'capacity': handover['capacity'],
            'periods': handover['periods'],
            'records': handover['records'],
            'risk_count': handover['risk_count'],
            'record_count': handover['record_count'],
            'confirmed_count': handover['confirmed_count'],
            'crossing_count': handover['crossing_count'],
            'redecided': redecided or [],
            'supersedes': prior['sheet_no'] if prior else None,
            'dispositions': (prior or {}).get('dispositions', []),
        }
        db.execute(
            'INSERT INTO handovers(business_day,sheet_no,run_id,state,body) VALUES (?,?,?,?,?)',
            (business_day, sheet_no, run_id, DRAFT,
             json.dumps(sheet, ensure_ascii=False)))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return get_sheet(business_day, sheet_no)


def sign(business_day, sheet_no):
    """值班签收：仅待签收单可签收，已签收/已替换的归档不可改动。"""
    with connection() as db:
        cur = db.execute(
            'UPDATE handovers SET state=? WHERE business_day=? AND sheet_no=? AND state=?',
            (SIGNED, business_day, sheet_no, DRAFT))
        if cur.rowcount != 1:
            if not db.execute('SELECT 1 FROM handovers WHERE business_day=? AND sheet_no=?',
                              (business_day, sheet_no)).fetchone():
                raise DomainError('NOT_FOUND', '交接单不存在', 404)
            raise DomainError('SHEET_STATE', '该交接单不可签收（已签收或已替换）', 409)
    return get_sheet(business_day, sheet_no)


def update_disposition(business_day, sheet_no, record_id, status, note):
    """在当前交接单上沿同一处置记录追踪处置状态。

    处置记录以 record_id（交接单内 R{seq}）定位；补发新单时 dispositions
    随版本链携带，同一场连续风险始终沿一条记录追踪。
    已签收/已替换的归档只追加新版本，不接受就地修改。
    """
    if status not in ('待处置', '处置中', '已处置'):
        raise DomainError('DISPOSITION', '处置状态无效')
    db = connection()
    try:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute(
            'SELECT * FROM handovers WHERE business_day=? AND sheet_no=?',
            (business_day, sheet_no)).fetchone()
        if not row:
            raise DomainError('NOT_FOUND', '交接单不存在', 404)
        if row['state'] != DRAFT:
            raise DomainError('SHEET_STATE', '已签收归档不可修改，请补发新版交接单', 409)
        sheet = json.loads(row['body'])
        known = {r['record_id'] for r in sheet.get('dispositions', [])}
        valid = {'R%d' % r['seq'] for r in sheet['records']}
        if record_id not in valid:
            raise DomainError('RECORD', '处置记录不存在于本交接单')
        if record_id not in known:
            sheet.setdefault('dispositions', []).append(
                {'record_id': record_id, 'status': status, 'note': note or ''})
        else:
            for item in sheet['dispositions']:
                if item['record_id'] == record_id:
                    item['status'], item['note'] = status, note or ''
        db.execute('UPDATE handovers SET body=? WHERE business_day=? AND sheet_no=?',
                   (json.dumps(sheet, ensure_ascii=False), business_day, sheet_no))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return get_sheet(business_day, sheet_no)


def ensure_table():
    with connection() as db:
        db.execute(
            'CREATE TABLE IF NOT EXISTS handovers ('
            'business_day TEXT NOT NULL, sheet_no INTEGER NOT NULL, run_id TEXT NOT NULL, '
            'state TEXT NOT NULL, body TEXT NOT NULL, '
            'PRIMARY KEY(business_day,sheet_no))')
        db.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_handovers_run '
            'ON handovers(business_day,run_id)')
