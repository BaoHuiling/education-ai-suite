import React, { useEffect, useState } from 'react';
import { Line } from 'react-chartjs-2';
import { Chart, registerables } from 'chart.js';
import Accordion from '../common/Accordion'; 
import '../../assets/css/RightPanel.css'
import '../../assets/css/MonitoringPausedBanner.css';
import { useTranslation } from 'react-i18next';
import { useAppSelector } from '../../redux/hooks';
import { useResourceMetricTimer } from '../../hooks/useResourceMetricTimer';
import MonitoringPausedBanner from '../common/MonitoringPausedBanner';
import { parseServerTimestamp } from '../../utils/serverTime';
Chart.register(...registerables);

interface ResourceUtilizationAccordionProps {
  activeScreen?: 'main' | 'content-search';
}

const ResourceUtilizationAccordion: React.FC<ResourceUtilizationAccordionProps> = ({ activeScreen = 'main' }) => {
  const { t } = useTranslation();
  const sessionId = useAppSelector(s => s.ui.sessionId);
  const monitoringPaused = useAppSelector(s => s.ui.monitoringPaused);
  const resourceMetrics = useAppSelector(s => s.resource?.metrics);
  const lastUpdated = useAppSelector(s => s.resource?.lastUpdated);
  const { resumeMonitoring } = useResourceMetricTimer();
  
  const [resourceData, setResourceData] = useState<any>({
    cpu_utilization: [],
    gpu_utilization: [],
    npu_utilization: [],
    memory: [],
    power: []
  });

  useEffect(() => {
    if (resourceMetrics && lastUpdated) {
      setResourceData(resourceMetrics);
    }
  }, [resourceMetrics, lastUpdated]);

  const createSimpleChartData = (data: any[], label: string, color: string) => {
    if (!data || data.length === 0) return { labels: [], datasets: [] };

    const labels = data.map((item: any) => item[0] ? parseServerTimestamp(item[0]).toLocaleTimeString() : '');

    return {
      labels,
      datasets: [{
        label,
        data: data.map((item: any) => item[1] || 0),
        borderColor: color,
        backgroundColor: color.replace('1)', '0.2)'),
        fill: false,
      }]
    };
  };

  const percentageChartOptions = {
    responsive: true,
    maintainAspectRatio: false,
    scales: {
      y: {
        type: 'linear' as const,
        display: true,
        position: 'left' as const,
        beginAtZero: true,
        min: 0,
        max: 100, 
        ticks: {
          stepSize: 20,
          callback: function(value: any) {
            return value;
          }
        }
      },
    },
    plugins: {
      legend: {
        display: true,
        position: 'top' as const,
      },
    },
  };


  const powerChartOptions = {
    responsive: true,
    maintainAspectRatio: false,
    scales: {
      y: {
        beginAtZero: true,
        min: 0,
        max: 500, 
        title: {
          display: true,
          text: 'Watts'
        },
        ticks: {
          stepSize: 100,
          callback: function(value: any) {
            return value ;
          }
        }
      }
    },
    plugins: {
      legend: {
        display: true,
        position: 'top' as const,
      },
    },
  };

  return (
    <Accordion title={t('accordion.resourceUtilization') || "Resource Utilization"}>
      <div className="accordion-subtitle">
        {t('accordion.subtitle_resource') || "System resource monitoring during AI processing"}
      </div>

      {monitoringPaused && (
        <MonitoringPausedBanner onResume={resumeMonitoring} />
      )}

      <div className="accordion-content">
        {sessionId ? (
          <>
            {/* CPU Utilization */}
            <div className="chart-section">
              <h4>{t('accordion.cpuUtilization') || "CPU Utilization"}</h4>
              <div style={{ height: '200px' }}>
                {resourceData.cpu_utilization && resourceData.cpu_utilization.length > 0 ? (
                  <Line 
                    data={createSimpleChartData(resourceData.cpu_utilization, 'CPU %', 'rgba(255, 99, 132, 1)')} 
                    options={percentageChartOptions} 
                  />
                ) : (
                  <p>{t('accordion.noData') || "No data available"}</p>
                )}
              </div>
            </div>

            {/* GPU Utilization */}
            <div className="chart-section">
              <h4>{t('accordion.gpuUtilization') || "GPU Utilization"}</h4>
              <div style={{ height: '200px' }}>
                {resourceData.gpu_utilization && resourceData.gpu_utilization.length > 0 ? (
                  <Line 
                    data={createSimpleChartData(resourceData.gpu_utilization, 'GPU %', 'rgba(54, 162, 235, 1)')} 
                    options={percentageChartOptions} 
                  />
                ) : (
                  <p>{t('accordion.noData') || "No data available"}</p>
                )}
              </div>
            </div>

            {/* NPU Utilization */}
            <div className="chart-section">
              <h4>{t('accordion.npuUtilization') || "NPU Utilization"}</h4>
              <div style={{ height: '200px' }}>
                {resourceData.npu_utilization && resourceData.npu_utilization.length > 0 ? (
                  <Line 
                    data={createSimpleChartData(resourceData.npu_utilization, 'NPU %', 'rgba(255, 159, 64, 1)')} 
                    options={percentageChartOptions} 
                  />
                ) : (
                  <p>{t('accordion.noData') || "No data available"}</p>
                )}
              </div>
            </div>

            {/* Memory Usage */}
            <div className="chart-section">
              <h4>{t('accordion.memoryUtilization') || "Memory Usage"}</h4>
              <div style={{ height: '200px' }}>
                {resourceData.memory && resourceData.memory.length > 0 ? (
                  <Line 
                    data={createSimpleChartData(resourceData.memory, 'Memory %', 'rgba(54, 162, 235, 1)')} 
                    options={percentageChartOptions} 
                  />
                ) : (
                  <p>{t('accordion.noData') || "No data available"}</p>
                )}
              </div>
            </div>

            {/* Power Consumption */}
            <div className="chart-section">
              <h4>{t('accordion.powerUtilization') || "Power Consumption"}</h4>
              <div style={{ height: '200px' }}>
                {resourceData.power && resourceData.power.length > 0 ? (
                  <Line 
                    data={createSimpleChartData(resourceData.power, 'Power (W)', 'rgba(75, 192, 192, 1)')} 
                    options={powerChartOptions} 
                  />
                ) : (
                  <p>{t('accordion.noData') || "No data available"}</p>
                )}
              </div>
            </div>

            {lastUpdated && (
              <p className="last-updated">
                {t('accordion.lastUpdated') || "Last updated"}: {new Date(lastUpdated).toLocaleTimeString()}
              </p>
            )}
          </>
        ) : (
          <div style={{ padding: '20px', textAlign: 'center' }}>
            <p>
              {activeScreen === 'content-search'
                ? t('accordion.noSessionActiveContentSearch', 'No active session. Upload files to begin monitoring.')
                : (t('accordion.noSessionActive') || 'No active session. Upload an audio file and start transcription to begin monitoring.')}
            </p>
            <small style={{ color: '#666' }}>
              Session ID: {sessionId || 'Not set'}
            </small>
          </div>
        )}
      </div>
    </Accordion>
  );
};

export default ResourceUtilizationAccordion;