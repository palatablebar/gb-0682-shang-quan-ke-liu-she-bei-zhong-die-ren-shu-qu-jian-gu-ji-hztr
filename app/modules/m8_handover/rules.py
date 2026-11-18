"""晚班交接判定规则。

裁决口径（风险时段、人数区间、处置状态共用同一判定）：
- 下界 > 容量线：已超限（CONFIRMED），上一班已证实的拥挤；
- 下界 <= 容量线 < 上界：跨线待证（CROSSING），区间跨过容量线但未证实，
  按下界会从值班页消失、按上界会被写成已超限，因此单列第三态进入交接；
- 上界 <= 容量线：未超限（不进入交接清单）。
同一场连续的风险时段合并为一条处置记录，沿同一 record_id 追踪。
"""
from datetime import datetime
from app.errors import DomainError

# 容量线：样例未提供独立主数据时的默认值，可用参数 capacity 覆盖。
DEFAULT_CAPACITY = 240

STATUS_CONFIRMED = '已超限'
STATUS_CROSSING = '跨线待证'
STATUS_CLEAR = '未超限'


def capacity_of(parameters):
    raw = (parameters or {}).get('capacity', DEFAULT_CAPACITY)
    if isinstance(raw, bool) or isinstance(raw, float):
        raise DomainError('CAPACITY', '容量线必须为正整数')
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise DomainError('CAPACITY', '容量线必须为正整数')
    if value <= 0:
        raise DomainError('CAPACITY', '容量线必须为正整数')
    return value


def classify(lower, upper, capacity):
    """单一裁决函数：页面清单、交接记录与处置状态都从这里取结论。"""
    if lower > capacity:
        return STATUS_CONFIRMED
    if upper > capacity:
        return STATUS_CROSSING
    return STATUS_CLEAR


def hour_of(bucket):
    # UTC 小时桶形如 2026-11-16T01:00:00+00:00，返回可跨午夜比较的绝对小时序号。
    try:
        moment = datetime.fromisoformat(bucket)
    except ValueError:
        raise DomainError('BUCKET', '小时桶格式无法识别: %s' % bucket)
    return moment.toordinal() * 24 + moment.hour


def judge_periods(state):
    """对 m5 的人数区间逐桶裁决，返回带统一判定的风险时段列表。"""
    capacity = capacity_of(state.get('parameters'))
    periods = []
    for row in state.get('calculations', []):
        lower, upper = row['lower'], row['upper']
        status = classify(lower, upper, capacity)
        if status == STATUS_CLEAR:
            continue
        periods.append({
            'bucket': row['id'],
            'hour': hour_of(row['id']),
            'lower': lower,
            'upper': upper,
            'capacity': capacity,
            'status': status,
        })
    periods.sort(key=lambda p: p['bucket'])
    return capacity, periods


def merge_records(periods):
    """同一场连续风险合并：相邻 UTC 小时且状态相容的时段归一条处置记录。"""
    records = []
    current = None
    for period in periods:
        contiguous = (
            current is not None
            and period['hour'] == current['end_hour'] + 1
            and period['status'] == current['status']
        )
        if not contiguous:
            if current is not None:
                records.append(current)
            current = {
                'venue': '主场',
                'status': period['status'],
                'start_bucket': period['bucket'],
                'end_bucket': period['bucket'],
                'start_hour': period['hour'],
                'end_hour': period['hour'],
                'lower_min': period['lower'],
                'upper_max': period['upper'],
                'capacity': period['capacity'],
                'periods': [],
            }
        else:
            current['end_bucket'] = period['bucket']
            current['end_hour'] = period['hour']
            current['upper_max'] = max(current['upper_max'], period['upper'])
            current['lower_min'] = min(current['lower_min'], period['lower'])
        current['periods'].append(period['bucket'])
    if current is not None:
        records.append(current)
    for index, record in enumerate(records, 1):
        record['seq'] = index
    return records


def diff_periods(old_periods, new_periods):
    """晚到数据重判定：只列出发生变化的时段，未变化的不重复打扰。"""
    old = {p['bucket']: p for p in old_periods}
    new = {p['bucket']: p for p in new_periods}
    changed = []
    for bucket in sorted(set(old) | set(new)):
        before, after = old.get(bucket), new.get(bucket)
        if before == after:
            continue
        changed.append({
            'bucket': bucket,
            'before': None if before is None else {
                'lower': before['lower'], 'upper': before['upper'], 'status': before['status'],
            },
            'after': None if after is None else {
                'lower': after['lower'], 'upper': after['upper'], 'status': after['status'],
            },
            'change': '新增风险' if before is None else '解除风险' if after is None else '结论变化',
        })
    return changed


def apply(state):
    capacity, periods = judge_periods(state)
    records = merge_records(periods)
    state['handover'] = {
        'capacity': capacity,
        'periods': periods,
        'records': records,
        'risk_count': len(periods),
        'record_count': len(records),
        'confirmed_count': sum(1 for p in periods if p['status'] == STATUS_CONFIRMED),
        'crossing_count': sum(1 for p in periods if p['status'] == STATUS_CROSSING),
    }
    return state
