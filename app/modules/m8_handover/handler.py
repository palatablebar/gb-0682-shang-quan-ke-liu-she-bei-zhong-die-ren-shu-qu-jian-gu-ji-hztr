from datetime import datetime, timezone, timedelta
import base64
from flask import Blueprint, jsonify, request, render_template
from app.web import api, page
from app.config import TITLE, MODULES, SHORT
from app.errors import DomainError
from app import store as run_store
from app.modules.m8_handover import storage
from app.modules.m8_handover.rules import diff_periods

bp = Blueprint('m8_handover', __name__)
SHANGHAI = timezone(timedelta(hours= 8))


def ensure():
    storage.ensure_table()


def business_day_of(state):
    """业务日取首批观测所在的上海自然日；无观测时退回当天。"""
    buckets = [c['id'] for c in state.get('calculations', [])]
    if not buckets:
        return datetime.now(SHANGHAI).date().isoformat()
    first = datetime.fromisoformat(min(buckets))
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    return first.astimezone(SHANGHAI).date().isoformat()


def attach_record_ids(sheet):
    """处置记录编号在补发时固化到记录体，页面与处置接口共用同一编号。"""
    for record in sheet.get('records', []):
        record['record_id'] = 'R%d' % record['seq']
    disposition_map = {d['record_id']: d for d in sheet.get('dispositions', [])}
    for record in sheet.get('records', []):
        item = disposition_map.get(record['record_id'])
        record['disposition'] = item['status'] if item else '待处置'
        record['disposition_note'] = item['note'] if item else ''
    return sheet


def handover_of(state):
    """取批次的交接判定；旧批次只跑到 M7 时按同源输入确定性补算（run_id 不变）。"""
    if state.get('handover'):
        return state['handover']
    if not state.get('source_b64'):
        raise DomainError('STAGE', '该批次缺少交接判定结果且无法重建')
    from app.workflow import run
    params = state.get('manifest', {}).get('parameters', {})
    rebuilt = run(base64.b64decode(state['source_b64']), params, 8)
    return rebuilt['handover']


@bp.get('/m8_handover')
def screen():
    ensure()
    from flask import render_template
    from app.web import payload
    content, parameters = payload()
    from app.workflow import run
    data = run(content, parameters, 8)
    latest = run_store.history()
    state = run_store.get(latest[0]['id']) if latest else data
    business_day = business_day_of(state)
    sheets = [attach_record_ids(s) for s in storage.list_sheets(business_day)]
    return render_template('m8_handover.html', title=TITLE, short=SHORT, modules=MODULES,
                           current=8, data=data, run=state, history=latest,
                           sheets=sheets, business_day=business_day)


@bp.route('/api/m8_handover', methods=['GET', 'POST'])
def preview():
    rendered = api(8)
    payload = rendered.get_json()
    for record in payload.get('handover', {}).get('records', []):
        record['record_id'] = 'R%d' % record['seq']
        record.setdefault('disposition', '待处置')
        record.setdefault('disposition_note', '')
    return jsonify(payload)


@bp.get('/api/handovers')
def list_sheets():
    ensure()
    return jsonify(storage.list_sheets(request.args.get('business_day')))


@bp.get('/api/handovers/<business_day>/<int:sheet_no>')
def get_sheet(business_day, sheet_no):
    ensure()
    return jsonify(attach_record_ids(storage.get_sheet(business_day, sheet_no)))


@bp.post('/api/handovers')
def issue_sheet():
    """晚到数据补发交接单。

    以 run_id 定位重算结果；与该业务日上一版交接单对比列出重新判定的时段。
    同 run_id 重发幂等返回旧单；已签收归档不被改写，补发失败保留旧版。
    """
    ensure()
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        raise DomainError('PAYLOAD', '请求必须是对象')
    run_id = data.get('run_id')
    if not isinstance(run_id, str) or not run_id:
        raise DomainError('RUN', '请提交运行批次 run_id')
    state = run_store.get(run_id)
    handover = handover_of(state)
    business_day = data.get('business_day') or business_day_of(state)
    redecided = []
    prior = storage.latest_sheet(business_day)
    if prior is not None:
        redecided = diff_periods(prior['periods'], handover['periods'])
    sheet = storage.issue(business_day, run_id, handover, redecided)
    return jsonify(attach_record_ids(sheet))


@bp.post('/api/handovers/<business_day>/<int:sheet_no>/sign')
def sign_sheet(business_day, sheet_no):
    ensure()
    return jsonify(attach_record_ids(storage.sign(business_day, sheet_no)))


@bp.post('/api/handovers/<business_day>/<int:sheet_no>/dispositions')
def disposition(business_day, sheet_no):
    ensure()
    data = request.get_json(silent=True) or {}
    record_id = data.get('record_id')
    if not isinstance(record_id, str) or not record_id:
        raise DomainError('RECORD', '请提交处置记录 record_id')
    sheet = storage.update_disposition(
        business_day, sheet_no, record_id,
        data.get('status', '处置中'), data.get('note', ''))
    return jsonify(attach_record_ids(sheet))
