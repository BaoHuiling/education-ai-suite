import React, { useCallback, useEffect, useState } from 'react';
import TopPanel from './components/TopPanel/TopPanel';
import HeaderBar from './components/Header/Header';
import Body from './components/common/Body';
import Footer from './components/Footer/Footer';
import './App.css';
import './assets/css/HeaderBar.css';
import MetricsPoller from './components/common/MetricsPoller';
import { getSettings, pingBackend } from './services/api';
import { useVideoPipelineMonitor } from "../src/redux/videoMonitor";
import { useAudioPipeline } from './redux/useAudioPipeline';
import { useTranslation } from 'react-i18next';
import { useFeatureConfig } from './hooks/useFeatureConfig';
  
// Real startup requires the backend health and feature config to load before
// the app renders the main UI. The preview override must stay disabled for the
// actual Smart Classroom flow.
const DEV_UI_PREVIEW = false;

const App: React.FC = () => {
  const { t } = useTranslation();
  const [projectName, setProjectName] = useState<string>('');
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [backendStatus, setBackendStatus] = useState<'checking' | 'available' | 'unavailable'>('checking');
  const [activeScreen, setActiveScreen] = useState<'main' | 'content-search'>('main');
  useVideoPipelineMonitor();
  // Both pipelines are driven from here, not from the panels that display them,
  // so they keep running while the user moves around the UI.
  useAudioPipeline();

  // Load feature configuration
  const { guard, loaded: featuresLoaded, loading: featuresLoading, error: featuresError } = useFeatureConfig();

  // Check if any main features are enabled
  const hasMainFeatures = featuresLoaded && guard ? 
    ['asr', 'summary', 'mindmap', 'topic_segmentation', 'video_analytics'].some(f => guard.hasFeature(f)) : 
    true; // Default to true during loading

  // Auto-switch to content-search screen if only content-search features are enabled
  useEffect(() => {
    if (!featuresLoaded || !guard) return;

    const mainFeatures = ['asr', 'summary', 'mindmap', 'topic_segmentation', 'video_analytics'];
    const contentSearchFeatures = ['content_search', 'qa'];

    const hasMainFeature = mainFeatures.some(f => guard.hasFeature(f));
    const hasContentSearchFeature = contentSearchFeatures.some(f => guard.hasFeature(f));

    // If main features are disabled, auto-switch based on what's available
    if (!hasMainFeature && hasContentSearchFeature) {
      console.log('📋 Main features disabled, content-search enabled - auto-switching to content-search screen');
      setActiveScreen('content-search');
    }
  }, [featuresLoaded, guard]);

  const openScreen = (screen: typeof activeScreen) => {
    setActiveScreen(screen);
  };

  // Both are memoised so the effects below can depend on them by name without
  // re-running on every render.
  const loadSettings = useCallback(async () => {
    try {
      const settings = await getSettings();
      if (settings.projectName) setProjectName(settings.projectName);
    } catch {
      console.warn('Failed to fetch project settings');
    }
  }, []);

  const checkBackendHealth = useCallback(async () => {
    try {
      const isHealthy = await pingBackend();

      if (isHealthy) {
        setBackendStatus('available');
        loadSettings();
        return;
      }

      setBackendStatus('unavailable');
    } catch {
      setBackendStatus('unavailable');
    }
  }, [loadSettings]);

  useEffect(() => {
    checkBackendHealth();
  }, [checkBackendHealth]);

  useEffect(() => {
    if (backendStatus === 'available') return;

    const interval = setInterval(checkBackendHealth, 5000);
    return () => clearInterval(interval);
  }, [backendStatus, checkBackendHealth]);


  if (!DEV_UI_PREVIEW && backendStatus === 'checking') {
    return (
      <div className="app-loading">
        <div className="loading-content">
          <div className="app-spinner" />
          <h2>{t('app.checkingBackendTitle')}</h2>
          <p>{t('app.checkingBackendMessage')}</p>
        </div>
      </div>
    );
  }

  if (!DEV_UI_PREVIEW && backendStatus === 'unavailable') {
    return (
      <div className="app-error">
        <div className="error-content">
          <h1>{t('app.backendUnavailableTitle')}</h1>
          <p>
            {t('app.backendUnavailableMessage')}
          </p>
        </div>
      </div>
    );
  }

  // Wait for features to load before rendering main UI
  if (!DEV_UI_PREVIEW && (featuresLoading || !featuresLoaded)) {
    return (
      <div className="app-loading">
        <div className="loading-content">
          <div className="app-spinner" />
          <h2>{t('app.loadingConfigTitle')}</h2>
          <p>{t('app.loadingConfigMessage')}</p>
        </div>
      </div>
    );
  }

  if (!DEV_UI_PREVIEW && featuresError) {
    return (
      <div className="app-error">
        <div className="error-content">
          <h1>{t('app.configErrorTitle')}</h1>
          <p>{featuresError}</p>
          <p>{t('app.configErrorMessage')}</p>
        </div>
      </div>
    );
  }


  return (
    <div className="app">
      <MetricsPoller />
      <TopPanel
        projectName={projectName}
        setProjectName={setProjectName}
        isSettingsOpen={isSettingsOpen}
        setIsSettingsOpen={setIsSettingsOpen}
        activeScreen={activeScreen}
        setActiveScreen={openScreen}
        featureGuard={guard}
        hasMainFeatures={hasMainFeatures}
      />
      <div style={{ display: activeScreen === 'main' ? 'contents' : 'none' }}>
        <HeaderBar projectName={projectName} setProjectName={setProjectName} featureGuard={guard} />
      </div>
      {activeScreen === 'content-search' && (
        <div className="content-search-subheader">
          <span>{t('contentSearch.subtitle')}</span>
        </div>
      )}
      <div className="main-content">
        <Body isModalOpen={isSettingsOpen} activeScreen={activeScreen} featureGuard={guard} hasMainFeatures={hasMainFeatures} />
      </div>
      <Footer />
    </div>
  );
};

export default App;