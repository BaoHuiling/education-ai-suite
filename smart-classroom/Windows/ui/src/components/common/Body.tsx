import React, { useState } from "react";
import LeftPanel from "../LeftPanel/LeftPanel";
import RightPanel from "../RightPanel/RightPanel";
import ContentSearchPanel from "../LeftPanel/ContentSearchPanel";
import "../../assets/css/Body.css";
import { usePanelDividerX } from "../../hooks/usePanelDividerX";
import PanelToggle from "./PanelToggle";
import type { FeatureGuard } from "../../utils/featureGuards";

interface BodyProps {
  activeScreen: 'main' | 'content-search' | 'grading';
  featureGuard: FeatureGuard;
  hasMainFeatures: boolean;
  onOpenConfig?: (path: string) => void;
}

const Body: React.FC<BodyProps> = ({ activeScreen, featureGuard, hasMainFeatures, onOpenConfig }) => {
  const [isRightPanelCollapsed, setIsRightPanelCollapsed] = useState(false);
  const toggleRightPanel = () => setIsRightPanelCollapsed(!isRightPanelCollapsed);
  const { containerRef, panelRef, arrowLeft, arrowTransition } =
    usePanelDividerX(isRightPanelCollapsed);

  // Show ContentSearchPanel if either content_search OR qa feature is enabled
  const hasContentSearchFeatures = featureGuard.hasFeature('content_search') || featureGuard.hasFeature('qa');

  return (
    <div className="container" ref={containerRef}>
      <div className="left-panel">
        <div style={{ display: activeScreen === 'main' ? 'contents' : 'none' }}>
          <LeftPanel featureGuard={featureGuard} />
        </div>
        <div style={{ display: activeScreen === 'content-search' ? 'contents' : 'none' }}>
          {hasContentSearchFeatures && (
            <ContentSearchPanel active={activeScreen === 'content-search'} />
          )}
        </div>
      </div>
      <div
        className="right-panel-slot"
        ref={panelRef}
        style={{ flex: isRightPanelCollapsed ? 0 : 1 }}
      >
        <RightPanel activeScreen={activeScreen} featureGuard={featureGuard} onOpenConfig={onOpenConfig} />
      </div>
      <PanelToggle collapsed={isRightPanelCollapsed} left={arrowLeft} transition={arrowTransition} onToggle={toggleRightPanel} />
    </div>
  );
};

export default Body;