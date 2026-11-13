import React, { useEffect, useMemo, useState } from 'react';
import { Line } from 'react-chartjs-2';
import { Chart, registerables } from 'chart.js';
import type { ChartData, ChartOptions, Scale, TooltipItem } from 'chart.js';
import Accordion from '../common/Accordion';
import '../../assets/css/RightPanel.css';
import '../../assets/css/MonitoringPausedBanner.css';
import '../../assets/css/ResourceUtilization.css';
import { useTranslation } from 'react-i18next';
import { useAppSelector } from '../../redux/hooks';
import { useResourceMetricTimer } from '../../hooks/useResourceMetricTimer';
import { getPlatformInfo } from '../../services/api';
import MonitoringPausedBanner from '../common/MonitoringPausedBanner';
Chart.register(...registerables);

// ---------------------------------------------------------------------------
// Backend row layouts. Every /metrics row is [isoTimestamp, ...numeric columns]
// (see monitoring/monitor.py: monitor_logs).
// ---------------------------------------------------------------------------
type MetricRow = (string | number)[];

const COL = {
  value: 1, // cpu / npu / power
  mem: { totalGb: 1, availableGb: 2, usedGb: 3, percent: 4 },
  gpu: { sharedMb: 3, d3: 4, encode: 5, decode: 6, processing: 7, copy: 8, compute: 9 },
};

const num = (row: MetricRow, col: number) => Number(row[col]) || 0;

interface Sample { t: number; row: MetricRow }

const parseRows = (rows: MetricRow[] | undefined): Sample[] => {
  const out: Sample[] = [];
  for (const row of rows ?? []) {
    const t = row?.[0] ? Date.parse(String(row[0])) : NaN;
    if (!Number.isNaN(t)) out.push({ t, row });
  }
  return out;
};

// ---------------------------------------------------------------------------
// Downsampling: average into wall-clock-aligned buckets so long ranges stay
// readable and only the newest bucket changes between polls.
// ---------------------------------------------------------------------------
const MAX_POINTS = 150;
const BUCKET_STEPS_S = [2, 4, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];

const pickBucketMs = (spanMs: number) =>
  (BUCKET_STEPS_S.find(s => spanMs / (s * 1000) <= MAX_POINTS) ?? BUCKET_STEPS_S[BUCKET_STEPS_S.length - 1]) * 1000;

type Extractor = (row: MetricRow) => number;
interface Point { x: number; y: number }

const bucketSeries = (samples: Sample[], extractors: Extractor[], bucketMs: number): Point[][] => {
  const out: Point[][] = extractors.map(() => []);
  let key = NaN, tSum = 0, n = 0;
  let sums: number[] = [];
  const flush = () => {
    if (n) sums.forEach((s, i) => out[i].push({ x: tSum / n, y: s / n }));
  };
  for (const { t, row } of samples) {
    const k = Math.floor(t / bucketMs);
    if (k !== key) {
      flush();
      key = k; tSum = 0; n = 0;
      sums = extractors.map(() => 0);
    }
    extractors.forEach((f, i) => { sums[i] += f(row); });
    tSum += t; n++;
  }
  flush();
  return out;
};

interface Stats { current: number; average: number; peak: number }

const statsOf = (samples: Sample[], f: Extractor): Stats | null => {
  if (!samples.length) return null;
  let sum = 0, peak = -Infinity;
  for (const s of samples) { const v = f(s.row); sum += v; peak = Math.max(peak, v); }
  return { current: f(samples[samples.length - 1].row), average: sum / samples.length, peak };
};

/** Round up to an axis maximum whose half is also a round number. */
const niceCeil = (v: number) => {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  return ([1, 2, 3, 4, 5, 6, 8, 10].find(m => m * p >= v) ?? 10) * p;
};

// ---------------------------------------------------------------------------
// Metric definitions
// ---------------------------------------------------------------------------
type MetricKey = 'cpu' | 'gpu' | 'npu' | 'memory' | 'power';
type SourceKey = 'cpu_utilization' | 'gpu_utilization' | 'npu_utilization' | 'memory' | 'power';

const gpuEngineCols = Object.values(COL.gpu).filter(c => c !== COL.gpu.sharedMb);

const METRICS: Record<MetricKey, { source: SourceKey; color: string; value: Extractor }> = {
  cpu:    { source: 'cpu_utilization', color: '#0071c5', value: r => num(r, COL.value) },
  // Overall GPU % = busiest engine.
  gpu:    { source: 'gpu_utilization', color: '#0071c5', value: r => Math.max(...gpuEngineCols.map(c => num(r, c))) },
  npu:    { source: 'npu_utilization', color: '#e96115', value: r => num(r, COL.value) },
  memory: { source: 'memory',          color: '#8f5da2', value: r => num(r, COL.mem.percent) },
  power:  { source: 'power',           color: '#548b18', value: r => num(r, COL.value) },
};
const METRIC_ORDER: MetricKey[] = ['cpu', 'gpu', 'npu', 'memory', 'power'];

const GPU_ENGINES = [
  { col: COL.gpu.d3, label: '3D', color: '#0071c5' },
  { col: COL.gpu.compute, label: 'Compute', color: '#8f5da2' },
  { col: COL.gpu.decode, label: 'Video Decode', color: '#e96115' },
  { col: COL.gpu.processing, label: 'Video Processing', color: '#00a3a3' },
];

type RangeKey = '1m' | '5m' | 'session';
const RANGE_MS: Record<Exclude<RangeKey, 'session'>, number> = { '1m': 60_000, '5m': 300_000 };

const fmtPct = (v: number) => `${Math.round(v)}%`;
const fmtW = (v: number) => `${v.toFixed(1)} W`;
const fmtGb = (v: number) => `${v.toFixed(1)} GB`;
const fmtClock = (ms: number) =>
  new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
// Compact 24h form for axis labels, e.g. "11:42:15" / "11:42".
const fmtAxisClock = (ms: number, seconds: boolean) =>
  new Date(ms).toLocaleTimeString([], { hourCycle: 'h23', hour: '2-digit', minute: '2-digit', ...(seconds ? { second: '2-digit' } : {}) });
const withAlpha = (hex: string, a: number) =>
  `rgba(${parseInt(hex.slice(1, 3), 16)}, ${parseInt(hex.slice(3, 5), 16)}, ${parseInt(hex.slice(5, 7), 16)}, ${a})`;

// Time-axis step: ~4 clock-aligned labels across the visible range.
const X_TICK_STEPS_S = [10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];
const pickXStepMs = (spanMs: number) =>
  (X_TICK_STEPS_S.find(s => spanMs / (s * 1000) <= 4) ?? X_TICK_STEPS_S[X_TICK_STEPS_S.length - 1]) * 1000;

// ---------------------------------------------------------------------------
// Chart
// ---------------------------------------------------------------------------
interface Series { label: string; color: string; points: Point[]; fill?: boolean }

interface MetricChartProps {
  series: Series[];
  xMin: number;
  xMax: number;
  yMax: number;
  format: (v: number) => string;
  /** y tick label, e.g. "50%" / "20 W" */
  tick: (v: number) => string;
}

const Y_AXIS_WIDTH = 38; // fixed so plot areas line up across cards

const MetricChart: React.FC<MetricChartProps> = ({ series, xMin, xMax, yMax, format, tick }) => {
  const data = useMemo<ChartData<'line', Point[]>>(() => ({
    datasets: series.map(s => ({
      label: s.label,
      data: s.points,
      borderColor: s.color,
      backgroundColor: withAlpha(s.color, 0.12),
      borderWidth: 1.5,
      fill: s.fill ? 'origin' : false,
      pointRadius: 0,
      pointHoverRadius: 3,
      pointHitRadius: 6,
      cubicInterpolationMode: 'monotone' as const,
      // Let the stroke spill a few px past the plot area so a line sitting
      // exactly at 0% or 100% isn't cut in half by the edge.
      clip: 3,
    })),
  }), [series]);

  const options = useMemo<ChartOptions<'line'>>(() => {
    const xStep = pickXStepMs(xMax - xMin);
    const showSeconds = xStep < 60_000;
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      parsing: false,
      normalized: true,
      interaction: { mode: 'index', axis: 'x', intersect: false },
      scales: {
        x: {
          type: 'linear',
          min: xMin,
          max: xMax,
          border: { display: false },
          grid: { display: false },
          ticks: {
            maxRotation: 0,
            autoSkip: false,
            color: '#8a8d93',
            font: { size: 10 },
            callback: (v: string | number) => fmtAxisClock(Number(v), showSeconds),
          },
          // Clock-aligned ticks so labels scroll with the data instead of
          // being relabelled on every poll.
          afterBuildTicks: (scale: Scale) => {
            const ticks = [];
            for (let v = Math.ceil(xMin / xStep) * xStep; v <= xMax; v += xStep) ticks.push({ value: v });
            scale.ticks = ticks;
          },
        },
        y: {
          type: 'linear',
          min: 0,
          max: yMax,
          border: { display: false },
          grid: { color: '#eceef1', drawTicks: false },
          ticks: {
            stepSize: yMax / 2,
            padding: 6,
            color: '#8a8d93',
            font: { size: 10 },
            callback: (v: string | number) => tick(Number(v)),
          },
          afterFit: (scale: Scale) => { scale.width = Y_AXIS_WIDTH; },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#2b2c30',
          padding: 8,
          boxWidth: 8,
          boxHeight: 8,
          callbacks: {
            title: (items: TooltipItem<'line'>[]) => (items.length ? fmtClock(items[0].parsed.x ?? 0) : ''),
            label: (ctx: TooltipItem<'line'>) => ` ${ctx.dataset.label}: ${format(ctx.parsed.y ?? 0)}`,
          },
        },
      },
    };
  }, [xMin, xMax, yMax, format, tick]);

  return <Line data={data} options={options} />;
};

// ---------------------------------------------------------------------------
// Accordion
// ---------------------------------------------------------------------------
interface ResourceUtilizationAccordionProps {
  activeScreen?: 'main' | 'content-search' | 'grading';
}

interface PlatformInfo { Processor?: string; iGPU?: string; NPU?: string; Memory?: string }

const pctTick = (v: number) => `${v}%`;

/** 0-100% axis, extended when a series goes above 100% (GPU engines can). */
const pctYMax = (...seriesList: Point[][]) => {
  let peak = 0;
  for (const pts of seriesList) for (const p of pts) peak = Math.max(peak, p.y);
  return peak > 100 ? Math.ceil(peak / 50) * 50 : 100;
};
const wTick = (v: number) => `${v} W`;

const ResourceUtilizationAccordion: React.FC<ResourceUtilizationAccordionProps> = ({ activeScreen = 'main' }) => {
  const { t } = useTranslation();
  const sessionId = useAppSelector(s => s.ui.sessionId);
  const monitoringPaused = useAppSelector(s => s.ui.monitoringPaused);
  const metrics = useAppSelector(s => s.resource?.metrics);
  const lastUpdated = useAppSelector(s => s.resource?.lastUpdated);
  const { resumeMonitoring } = useResourceMetricTimer();

  const [range, setRange] = useState<RangeKey>('1m');
  const [platform, setPlatform] = useState<PlatformInfo | null>(null);

  useEffect(() => {
    getPlatformInfo().then(setPlatform).catch(() => { /* device names are optional */ });
  }, []);

  // Parse each source once per poll.
  const parsed = useMemo(() => {
    const out = {} as Record<SourceKey, Sample[]>;
    for (const m of METRIC_ORDER) {
      const src = METRICS[m].source;
      out[src] = parseRows(metrics?.[src] as MetricRow[] | undefined);
    }
    return out;
  }, [metrics]);

  // Shared time window, anchored to the newest sample (not "now"), so the
  // last minute stays visible after monitoring stops.
  const view = useMemo(() => {
    const all = Object.values(parsed).filter(s => s.length);
    if (!all.length) return null;
    const latest = Math.max(...all.map(s => s[s.length - 1].t));
    const earliest = Math.min(...all.map(s => s[0].t));
    const spanMs = range === 'session' ? Math.max(latest - earliest, 60_000) : RANGE_MS[range];
    const xMin = latest - spanMs;
    const bucketMs = pickBucketMs(spanMs);
    // windowed: samples inside the range (for stats).
    // plotted: same plus one lead-in sample before the range, so the line
    // reaches the left edge instead of starting a few pixels in.
    const windowed = {} as Record<SourceKey, Sample[]>;
    const plotted = {} as Record<SourceKey, Sample[]>;
    for (const [k, s] of Object.entries(parsed) as [SourceKey, Sample[]][]) {
      let i = s.findIndex(x => x.t >= xMin);
      if (i < 0) i = s.length;
      windowed[k] = s.slice(i);
      plotted[k] = s.slice(Math.max(0, i - 1));
    }
    return { xMin, xMax: latest, bucketMs, windowed, plotted };
  }, [parsed, range]);

  const series = useMemo(() => {
    if (!view) return null;
    const out = {} as Record<MetricKey, Point[]>;
    for (const m of METRIC_ORDER) {
      out[m] = bucketSeries(view.plotted[METRICS[m].source], [METRICS[m].value], view.bucketMs)[0];
    }
    return out;
  }, [view]);

  const gpuEngineSeries = useMemo(() => {
    if (!view) return null;
    return bucketSeries(view.plotted.gpu_utilization, GPU_ENGINES.map(e => (r: MetricRow) => num(r, e.col)), view.bucketMs);
  }, [view]);

  const stats = useMemo(() => {
    const out = {} as Record<MetricKey, Stats | null>;
    for (const m of METRIC_ORDER) {
      out[m] = view ? statsOf(view.windowed[METRICS[m].source], METRICS[m].value) : null;
    }
    return out;
  }, [view]);

  const lastRow = (src: SourceKey) => {
    const rows = view?.windowed[src] ?? [];
    return rows.length ? rows[rows.length - 1].row : null;
  };
  const lastMem = lastRow('memory');
  const lastGpu = lastRow('gpu_utilization');

  const powerYMax = niceCeil(Math.max(10, (stats.power?.peak ?? 0) * 1.1));

  const tr = (key: string, fallback: string) => t(`accordion.perf.${key}`, fallback);
  const dash = '—';
  const noData = t('accordion.noData') || 'No data available';

  const renderCard = (m: MetricKey, opts: {
    title: string;
    device?: string;
    format: (v: number) => string;
    tick: (v: number) => string;
    yMax: number;
    chartSeries: Series[];
    meta?: React.ReactNode;
    legend?: React.ReactNode;
  }) => {
    const s = stats[m];
    const { color } = METRICS[m];
    return (
      <section key={m} className="ru-card" style={{ '--ru-accent': color } as React.CSSProperties}>
        <header className="ru-card__header">
          <div className="ru-card__titles">
            <h4 className="ru-card__title">{opts.title}</h4>
            {opts.device && <span className="ru-card__device" title={opts.device}>{opts.device}</span>}
          </div>
          <div className="ru-card__value">{s ? opts.format(s.current) : dash}</div>
        </header>

        {s && (
          <div className="ru-card__meta">
            <span>{tr('average', 'Avg')} <b>{opts.format(s.average)}</b></span>
            <span>{tr('peak', 'Peak')} <b>{opts.format(s.peak)}</b></span>
            {opts.meta}
          </div>
        )}

        <div className="ru-card__chart">
          {s && view ? (
            <MetricChart series={opts.chartSeries} xMin={view.xMin} xMax={view.xMax}
              yMax={opts.yMax} format={opts.format} tick={opts.tick} />
          ) : (
            <div className="ru-card__empty">{noData}</div>
          )}
        </div>
        {s && opts.legend}
      </section>
    );
  };

  const renderCards = () => {
    if (!series || !gpuEngineSeries) return null;
    const single = (m: MetricKey, label: string): Series[] =>
      [{ label, color: METRICS[m].color, points: series[m], fill: true }];

    return (
      <div className="ru-cards">
        {renderCard('cpu', {
          title: t('accordion.cpuUtilization') || 'CPU Utilization',
          device: platform?.Processor,
          format: fmtPct, tick: pctTick, yMax: pctYMax(series.cpu),
          chartSeries: single('cpu', 'CPU'),
        })}

        {renderCard('gpu', {
          title: t('accordion.gpuUtilization') || 'GPU Utilization',
          device: platform?.iGPU,
          format: fmtPct, tick: pctTick, yMax: pctYMax(...gpuEngineSeries),
          chartSeries: GPU_ENGINES.map((e, i) => ({ label: e.label, color: e.color, points: gpuEngineSeries[i] })),
          meta: lastGpu && (
            <span>{tr('sharedMemory', 'Shared mem')} <b>{fmtGb(num(lastGpu, COL.gpu.sharedMb) / 1024)}</b></span>
          ),
          legend: (
            <ul className="ru-legend">
              {GPU_ENGINES.map(e => (
                <li key={e.col}>
                  <i style={{ background: e.color }} />
                  {e.label}
                  <b>{lastGpu ? fmtPct(num(lastGpu, e.col)) : dash}</b>
                </li>
              ))}
            </ul>
          ),
        })}

        {renderCard('npu', {
          title: t('accordion.npuUtilization') || 'NPU Utilization',
          device: platform?.NPU,
          format: fmtPct, tick: pctTick, yMax: pctYMax(series.npu),
          chartSeries: single('npu', 'NPU'),
        })}

        {renderCard('memory', {
          title: t('accordion.memoryUtilization') || 'Memory Usage',
          format: fmtPct, tick: pctTick, yMax: pctYMax(series.memory),
          chartSeries: single('memory', tr('memory', 'Memory')),
          meta: lastMem && (
            <span>
              {tr('used', 'Used')}{' '}
              <b>{num(lastMem, COL.mem.usedGb).toFixed(1)} / {fmtGb(num(lastMem, COL.mem.totalGb))}</b>
            </span>
          ),
        })}

        {renderCard('power', {
          title: t('accordion.powerUtilization') || 'Power Consumption',
          format: fmtW, tick: wTick, yMax: powerYMax,
          chartSeries: single('power', tr('power', 'Power')),
        })}
      </div>
    );
  };

  return (
    <Accordion title={t('accordion.resourceUtilization') || 'Resource Utilization'}>
      <div className="accordion-subtitle">
        {t('accordion.subtitle_resource') || 'System resource monitoring during AI processing'}
      </div>

      {monitoringPaused && <MonitoringPausedBanner onResume={resumeMonitoring} />}

      <div className="ru-root">
        {sessionId && view ? (
          <>
            <div className="ru-toolbar">
              <span className="ru-toolbar__updated">
                {lastUpdated ? `${t('accordion.lastUpdated') || 'Last updated'} ${fmtClock(lastUpdated)}` : ''}
              </span>
              <div className="ru-range" role="group" aria-label={tr('timeRange', 'Time range')}>
                {(['1m', '5m', 'session'] as RangeKey[]).map(r => (
                  <button key={r} type="button" aria-pressed={range === r}
                    className={range === r ? 'is-active' : ''} onClick={() => setRange(r)}>
                    {r === '1m' ? tr('range1m', '1 min') : r === '5m' ? tr('range5m', '5 min') : tr('rangeSession', 'Session')}
                  </button>
                ))}
              </div>
            </div>
            {renderCards()}
          </>
        ) : (
          <div className="ru-empty">
            <p>
              {!sessionId
                ? activeScreen === 'content-search'
                  ? t('accordion.noSessionActiveContentSearch', 'No active session. Upload files to begin monitoring.')
                  : (t('accordion.noSessionActive') || 'No active session. Upload an audio file and start transcription to begin monitoring.')
                : noData}
            </p>
          </div>
        )}
      </div>
    </Accordion>
  );
};

export default ResourceUtilizationAccordion;
