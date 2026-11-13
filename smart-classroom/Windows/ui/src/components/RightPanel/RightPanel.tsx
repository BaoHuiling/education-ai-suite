import React from "react";
import ConfigurationMetricsAccordion from "./ConfigurationMetricsAccordion";
import ResourceUtilizationAccordion from "./ResourceUtilizationAccordion";
import ClassStatisticsAccordion from './ClassEngagementAccordion';
import TimelineAccordion from './TimelineAccordion';
import "../../assets/css/RightPanel.css";
import type { FeatureGuard } from "../../utils/featureGuards";

interface RightPanelProps {
  activeScreen: 'main' | 'content-search' | 'grading';
  featureGuard: FeatureGuard;
  /** Opens Settings → Configuration on one field (config path). */
  onOpenConfig?: (path: string) => void;
}

const RightPanel: React.FC<RightPanelProps> = ({ activeScreen, featureGuard, onOpenConfig }) => {
  const hasASR = featureGuard.hasFeature('asr');
  const hasVideoAnalytics = featureGuard.hasFeature('video_analytics');
  const hasDiarization = featureGuard.isDiarizationEnabled();
  
  return (
    <div className="right-panel">
      <ConfigurationMetricsAccordion activeScreen={activeScreen} onOpenConfig={onOpenConfig} />
      <ResourceUtilizationAccordion activeScreen={activeScreen} />
      <div style={{ display: activeScreen === 'main' ? 'contents' : 'none' }}>
        {/* Show ClassStatisticsAccordion when both audio and video pipelines are enabled */}
        {hasASR && hasVideoAnalytics && <ClassStatisticsAccordion featureGuard={featureGuard} />}
        
        {/* Show standalone TimelineAccordion only when ASR is enabled, video analytics is NOT, and diarization is enabled */}
        {hasASR && !hasVideoAnalytics && hasDiarization && <TimelineAccordion />}
      </div>
    </div>
  );
};

export default RightPanel;