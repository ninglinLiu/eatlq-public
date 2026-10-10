DROP VIEW IF EXISTS eatlq.phase2_alert_summary;
DROP VIEW IF EXISTS eatlq.phase2_alert_events;

CREATE VIEW eatlq.phase2_alert_events
AS
SELECT
    'raw_archive' AS alert_scope,
    '' AS surface,
    'raw_lag_seconds' AS alert_kind,
    'critical' AS severity,
    toInt64(raw_lag_seconds) AS actual_value,
    toInt64(300) AS threshold_value,
    concat('raw archive lag seconds exceeds threshold: ', toString(raw_lag_seconds)) AS detail,
    'investigate-runtime' AS remediation_target,
    'bash scripts/phase2-healthcheck.sh 30 24 10' AS remediation_command
FROM eatlq.phase2_raw_status
WHERE raw_lag_seconds > 300

UNION ALL

SELECT
    'normalized_surface' AS alert_scope,
    surface,
    'recent_parity_gap' AS alert_kind,
    'critical' AS severity,
    recent_gap AS actual_value,
    toInt64(0) AS threshold_value,
    concat('recent raw vs normalized parity gap detected for ', surface, ': ', toString(recent_gap)) AS detail,
    multiIf(
        surface = 'erc20_transfers', 'erc20',
        surface = 'uniswap_v2_swaps', 'v2',
        surface = 'uniswap_v3_swaps', 'v3',
        surface = 'uniswap_v2_mints', 'v2-mint',
        surface = 'uniswap_v2_burns', 'v2-burn',
        surface = 'uniswap_v3_mints', 'v3-mint',
        surface = 'uniswap_v3_burns', 'v3-burn',
        'manual'
    ) AS remediation_target,
    concat(
        'bash scripts/rebuild-normalized-surface.sh ',
        multiIf(
            surface = 'erc20_transfers', 'erc20',
            surface = 'uniswap_v2_swaps', 'v2',
            surface = 'uniswap_v3_swaps', 'v3',
            surface = 'uniswap_v2_mints', 'v2-mint',
            surface = 'uniswap_v2_burns', 'v2-burn',
            surface = 'uniswap_v3_mints', 'v3-mint',
            surface = 'uniswap_v3_burns', 'v3-burn',
            'manual'
        )
    ) AS remediation_command
FROM eatlq.phase2_surface_status
WHERE recent_gap != 0

UNION ALL

SELECT
    'normalized_surface' AS alert_scope,
    surface,
    'duplicate_surplus' AS alert_kind,
    'critical' AS severity,
    toInt64(duplicate_surplus) AS actual_value,
    toInt64(0) AS threshold_value,
    concat('duplicate surplus detected for ', surface, ': ', toString(duplicate_surplus)) AS detail,
    multiIf(
        surface = 'erc20_transfers', 'erc20',
        surface = 'uniswap_v2_swaps', 'v2',
        surface = 'uniswap_v3_swaps', 'v3',
        surface = 'uniswap_v2_mints', 'v2-mint',
        surface = 'uniswap_v2_burns', 'v2-burn',
        surface = 'uniswap_v3_mints', 'v3-mint',
        surface = 'uniswap_v3_burns', 'v3-burn',
        'manual'
    ) AS remediation_target,
    concat(
        'bash scripts/rebuild-normalized-surface.sh ',
        multiIf(
            surface = 'erc20_transfers', 'erc20',
            surface = 'uniswap_v2_swaps', 'v2',
            surface = 'uniswap_v3_swaps', 'v3',
            surface = 'uniswap_v2_mints', 'v2-mint',
            surface = 'uniswap_v2_burns', 'v2-burn',
            surface = 'uniswap_v3_mints', 'v3-mint',
            surface = 'uniswap_v3_burns', 'v3-burn',
            'manual'
        )
    ) AS remediation_command
FROM eatlq.phase2_surface_status
WHERE duplicate_surplus > 0

UNION ALL

SELECT
    'normalized_surface' AS alert_scope,
    surface,
    'registry_gap' AS alert_kind,
    'critical' AS severity,
    uncovered_registry_gap AS actual_value,
    multiIf(surface = 'uniswap_v2_mints', toInt64(1), toInt64(0)) AS threshold_value,
    concat('lifecycle registry gap exceeds threshold for ', surface, ': ', toString(uncovered_registry_gap)) AS detail,
    'inspect-registry' AS remediation_target,
    'bash scripts/inspect-registry-gaps.sh' AS remediation_command
FROM eatlq.phase2_surface_status
WHERE surface IN ('uniswap_v2_mints', 'uniswap_v2_burns', 'uniswap_v3_mints', 'uniswap_v3_burns')
  AND uncovered_registry_gap > multiIf(surface = 'uniswap_v2_mints', toInt64(1), toInt64(0))

UNION ALL

SELECT
    'normalized_surface' AS alert_scope,
    surface,
    'historical_projectable_gap' AS alert_kind,
    'warning' AS severity,
    projectable_total_gap AS actual_value,
    toInt64(0) AS threshold_value,
    concat('historical projectable gap remains for ', surface, ': ', toString(projectable_total_gap)) AS detail,
    multiIf(
        surface = 'erc20_transfers', 'erc20',
        surface = 'uniswap_v2_swaps', 'v2',
        surface = 'uniswap_v3_swaps', 'v3',
        surface = 'uniswap_v2_mints', 'v2-mint',
        surface = 'uniswap_v2_burns', 'v2-burn',
        surface = 'uniswap_v3_mints', 'v3-mint',
        surface = 'uniswap_v3_burns', 'v3-burn',
        'manual'
    ) AS remediation_target,
    concat(
        'bash scripts/rebuild-normalized-surface.sh ',
        multiIf(
            surface = 'erc20_transfers', 'erc20',
            surface = 'uniswap_v2_swaps', 'v2',
            surface = 'uniswap_v3_swaps', 'v3',
            surface = 'uniswap_v2_mints', 'v2-mint',
            surface = 'uniswap_v2_burns', 'v2-burn',
            surface = 'uniswap_v3_mints', 'v3-mint',
            surface = 'uniswap_v3_burns', 'v3-burn',
            'manual'
        )
    ) AS remediation_command
FROM eatlq.phase2_surface_status
WHERE projectable_total_gap != 0

UNION ALL

SELECT
    'normalized_surface' AS alert_scope,
    surface,
    'historical_unprojectable_raw' AS alert_kind,
    'warning' AS severity,
    raw_unprojectable_total_rows AS actual_value,
    toInt64(0) AS threshold_value,
    concat('historical raw rows fail projection filter for ', surface, ': ', toString(raw_unprojectable_total_rows)) AS detail,
    'inspect-projection-filter' AS remediation_target,
    'bash scripts/inspect-phase2-status.sh' AS remediation_command
FROM eatlq.phase2_surface_status
WHERE raw_unprojectable_total_rows != 0;

CREATE VIEW eatlq.phase2_alert_summary
AS
SELECT
    severity,
    alert_kind,
    count() AS alert_count,
    groupArray(if(surface = '', alert_scope, concat(alert_scope, ':', surface))) AS targets
FROM eatlq.phase2_alert_events
GROUP BY severity, alert_kind
ORDER BY severity, alert_kind;
