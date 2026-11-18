"""M8 晚班交接：跨线裁决、连续风险合并、晚到数据版本链与补发回滚。"""
import pytest
from app.errors import DomainError
from app.workflow import run
from app import store
from app.modules.m8_handover.rules import (
    classify, diff_periods,
    STATUS_CONFIRMED, STATUS_CROSSING, STATUS_CLEAR,
)
from app.modules.m8_handover import storage
from app.modules.m8_handover.handler import business_day_of, attach_record_ids


@pytest.fixture
def state(raw):
    return run(raw, {}, stage=8)


def test_three_way_classification():
    # 同一容量线、同一裁决函数：下界视角与上界视角都不单独决定结论
    assert classify(250, 260, 240) == STATUS_CONFIRMED
    assert classify(230, 250, 240) == STATUS_CROSSING
    assert classify(200, 240, 240) == STATUS_CLEAR


def test_crossing_periods_enter_handover(state):
    handover = state['handover']
    statuses = {p['bucket']: p['status'] for p in handover['periods']}
    # dirty 样例下界 230、上界越过 240：必须进入交接而非消失，也不写成已超限
    assert all(v == STATUS_CROSSING for v in statuses.values())
    assert handover['confirmed_count'] == 0
    assert handover['crossing_count'] == handover['risk_count'] > 0


def test_capacity_parameter_changes_verdict(raw):
    loose = run(raw, {'capacity': 300}, stage=8)['handover']
    tight = run(raw, {'capacity': 100}, stage=8)['handover']
    assert loose['risk_count'] == 0
    assert tight['confirmed_count'] >= tight['crossing_count']
    assert tight['risk_count'] > 0


def test_invalid_capacity(raw):
    with pytest.raises(DomainError):
        run(raw, {'capacity': 0}, stage=8)
    with pytest.raises(DomainError):
        run(raw, {'capacity': '满'}, stage=8)


def test_contiguous_risk_merges_into_one_record(state):
    records = state['handover']['records']
    # 12 个连续小时同状态 -> 一条处置记录，时段数对齐
    assert len(records) == 1
    assert len(records[0]['periods']) == state['handover']['risk_count']
    assert records[0]['start_hour'] + 11 == records[0]['end_hour']


def test_contiguity_across_midnight():
    from app.modules.m8_handover.rules import hour_of, merge_records
    h1 = hour_of('2026-11-16T23:00:00+00:00')
    h2 = hour_of('2026-11-17T00:00:00+00:00')
    assert h2 == h1 + 1
    base = {'lower': 230, 'upper': 250, 'capacity': 240, 'status': STATUS_CROSSING}
    p1 = dict(base, bucket='2026-11-16T23:00:00+00:00', hour=h1)
    p2 = dict(base, bucket='2026-11-17T00:00:00+00:00', hour=h2)
    assert len(merge_records([p1, p2])) == 1
    p3 = dict(base, bucket='2026-11-17T01:00:00+00:00', hour=h2 + 1)
    assert len(merge_records([p1, p3])) == 2


def test_status_change_splits_records(raw):
    # 抬高两个相邻桶的 zone 计数，制造 已超限 段，验证状态切换时拆记录
    text = raw.decode('utf-8-sig').splitlines()
    bumped = []
    for line in text:
        cols = line.split(',')
        if len(cols) > 6 and 'T11:00:00+08:00' in cols[1] and cols[3] == 'zone':
            cols[6] = str(int(cols[6]) + 40)
        if len(cols) > 6 and 'T12:00:00+08:00' in cols[1] and cols[3] == 'zone':
            cols[6] = str(int(cols[6]) + 40)
        bumped.append(','.join(cols))
    out = ('﻿' + '\n'.join(bumped)).encode()
    handover = run(out, {}, stage=8)['handover']
    statuses = [r['status'] for r in handover['records']]
    assert STATUS_CONFIRMED in statuses
    assert len(handover['records']) >= 2
    # 每条记录内部时段连续
    for record in handover['records']:
        assert record['end_hour'] >= record['start_hour']
        assert record['periods'] == sorted(record['periods'])


def test_diff_lists_only_changed_periods(state):
    old = state['handover']['periods']
    changed = diff_periods(old, old)
    assert changed == []
    new = [dict(p) for p in old]
    new[0]['status'] = STATUS_CONFIRMED
    new[0]['lower'] = state['handover']['capacity'] + 1
    diff = diff_periods(old, new)
    assert len(diff) == 1 and diff[0]['change'] == '结论变化'
    dropped = diff_periods(old, old[1:])
    assert dropped[0]['change'] == '解除风险' and dropped[0]['after'] is None
    added = diff_periods(old[1:], old)
    assert added[0]['change'] == '新增风险' and added[0]['before'] is None


def test_sheet_issue_and_idempotent_rerun(state, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'h.db'))
    storage.ensure_table()
    store.save(state['run_id'], state)
    day = business_day_of(state)
    v1 = storage.issue(day, state['run_id'], state['handover'])
    assert v1['sheet_no'] == 1 and v1['supersedes'] is None
    # 同一批结果重跑：不新增风险段、不新增交接单
    again = storage.issue(day, state['run_id'], state['handover'])
    assert again['sheet_no'] == 1
    assert len(storage.list_sheets(day)) == 1


def test_signed_archive_not_rewritten(state, raw, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'h.db'))
    storage.ensure_table()
    store.save(state['run_id'], state)
    day = business_day_of(state)
    storage.issue(day, state['run_id'], state['handover'])
    storage.sign(day, 1)
    late = run(raw, {'capacity': 200}, stage=8)
    store.save(late['run_id'], late)
    v2 = storage.issue(day, late['run_id'], late['handover'])
    assert v2['sheet_no'] == 2 and v2['supersedes'] == 1
    v1 = storage.get_sheet(day, 1)
    # 已签收归档内容不变，仅状态沿版本链流转
    assert v1['periods'] == state['handover']['periods']
    assert v1['state'] == '已替换'
    assert v2['redecided']  # 页面能列出重新判定的时段
    with pytest.raises(DomainError) as exc:
        storage.sign(day, 1)
    assert exc.value.status == 409


def test_failed_reissue_keeps_old_sheet(state, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'h.db'))
    storage.ensure_table()
    store.save(state['run_id'], state)
    day = business_day_of(state)
    storage.issue(day, state['run_id'], state['handover'])
    storage.sign(day, 1)

    real_connection = storage.connection
    class Boom:
        def __init__(self, db): self.db = db
        def execute(self, sql, params=()):
            if sql.lstrip().upper().startswith('INSERT INTO HANDOVERS'):
                raise RuntimeError('storage failure')
            return self.db.execute(sql, params)
        def commit(self): return self.db.commit()
        def rollback(self): return self.db.rollback()
        def close(self): return self.db.close()
    storage.connection = lambda: Boom(real_connection())
    with pytest.raises(RuntimeError):
        storage.issue(day, 'z' * 20, state['handover'])
    storage.connection = real_connection
    sheets = storage.list_sheets(day)
    assert len(sheets) == 1
    assert storage.get_sheet(day, 1)['state'] == '已签收'


def test_disposition_tracks_single_record(state, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'h.db'))
    storage.ensure_table()
    store.save(state['run_id'], state)
    day = business_day_of(state)
    sheet = storage.issue(day, state['run_id'], state['handover'])
    record_id = 'R1'
    updated = storage.update_disposition(day, sheet['sheet_no'], record_id, '处置中', '加派引导')
    assert updated['dispositions'][0]['record_id'] == record_id
    again = storage.update_disposition(day, sheet['sheet_no'], record_id, '已处置', '客流回落')
    assert len(again['dispositions']) == 1 and again['dispositions'][0]['status'] == '已处置'
    with pytest.raises(DomainError):
        storage.update_disposition(day, sheet['sheet_no'], 'R99', '处置中', '')
    with pytest.raises(DomainError):
        storage.update_disposition(day, sheet['sheet_no'], record_id, '胡乱状态', '')


def test_disposition_carries_across_reissue(state, raw, tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'h.db'))
    storage.ensure_table()
    store.save(state['run_id'], state)
    day = business_day_of(state)
    v1 = storage.issue(day, state['run_id'], state['handover'])
    storage.update_disposition(day, v1['sheet_no'], 'R1', '处置中', '持续观察')
    storage.sign(day, 1)
    late = run(raw, {'capacity': 200}, stage=8)
    store.save(late['run_id'], late)
    v2 = storage.issue(day, late['run_id'], late['handover'])
    # 同一场连续风险沿一条处置记录追踪：处置随版本链携带
    assert v2['dispositions'][0]['record_id'] == 'R1'
    assert v2['dispositions'][0]['status'] == '处置中'
    rendered = attach_record_ids(v2)
    matched = [r for r in rendered['records'] if r['record_id'] == 'R1']
    assert matched and matched[0]['disposition'] == '处置中'


def test_http_handover_flow(client, state, monkeypatch, tmp_path):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'web.db'))
    storage.ensure_table()
    store.save(state['run_id'], state)
    day = business_day_of(state)
    page = client.get('/m8_handover')
    assert page.status_code == 200 and '跨线时段裁决'.encode() in page.data
    r = client.post('/api/handovers', json={'run_id': state['run_id']})
    assert r.status_code == 200
    sheet = r.get_json()
    assert sheet['sheet_no'] == 1 and sheet['state'] == '待签收'
    # 幂等重发：同批结果不新增交接单
    assert client.post('/api/handovers', json={'run_id': state['run_id']}).get_json()['sheet_no'] == 1
    # 签收
    assert client.post('/api/handovers/%s/1/sign' % day, json={}).status_code == 200
    listing = client.get('/api/handovers?business_day=' + day).get_json()
    assert len(listing) == 1 and listing[0]['state'] == '已签收'
    # 已签收归档不可就地改处置
    d = client.post('/api/handovers/%s/1/dispositions' % day,
                    json={'record_id': 'R1', 'status': '处置中', 'note': '引导'})
    assert d.status_code == 409
    # 缺少 run_id
    assert client.post('/api/handovers', json={}).status_code == 422
    # 未知批次
    assert client.post('/api/handovers', json={'run_id': 'nope'}).status_code == 404
