const STATE_LABELS = Object.freeze({
  completed: '已完成',
  failed: '失败',
  partial_success: '部分完成',
  pending: '未到期',
  success: '成功',
  unavailable: '暂无数据',
  verified: '已校验',
});

const REASON_LABELS = Object.freeze({
  dashboard_read_failed: '读取失败',
  fetch_unavailable: '当前环境无法读取数据',
  fixture_3d_not_mature: '未到期',
  fixture_partial_result: '部分结果',
  insufficient_evidence_zero_samples: '暂无数据',
  manifest_hash_mismatch: '数据校验失败',
  merged_20d_chain_unavailable: '未到期',
  outcome_20d_not_mature: '未到期',
  outcome_state_unavailable: '暂无数据',
  public_candidate_outcome_unavailable: '暂无数据',
  public_fetch_failed: '数据源暂不可用',
  public_http_error: '数据源暂不可用',
  public_json_invalid: '数据格式异常',
  public_short_term_candidate_rank_unavailable: '暂无公开排名',
});

export const stateLabel = (state) => STATE_LABELS[state] ?? state;

export function reasonLabel(reasonCode, state = 'unavailable') {
  if (REASON_LABELS[reasonCode]) return REASON_LABELS[reasonCode];
  if (state === 'pending') return '未到期';
  if (state === 'failed' || state === 'failure') return '读取失败';
  return '暂无数据';
}

export function sourceLabel(url) {
  if (url.startsWith('fixture://')) return '虚构 Dashboard fixture';
  if (url.includes('/research/results/')) return '脱敏 research/results 研究结果';
  if (url.includes('/screening-results/')) return 'Public screening-results 已发布结果';
  return '公开数据结果';
}
