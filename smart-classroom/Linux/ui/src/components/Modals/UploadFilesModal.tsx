import React, { useRef, useState } from 'react';
import Modal from './Modal';
import '../../assets/css/UploadFilesModal.css';
import folderIcon from '../../assets/images/folder.svg';
import {
  startVideoAnalyticsPipeline,
  uploadAudio,
  uploadVideo,
  storeAudioDuration,
  createSession,
  startPipelineMonitoring,
  BACKEND_UNAVAILABLE_MESSAGE
} from '../../services/api';
import { useAppDispatch, useAppSelector } from '../../redux/hooks';
import {
  setUploadedAudioPath,
  startProcessing,
  processingFailed,
  resetFlow,
  setSessionId,
  setActiveStream,
  startStream,
  setFrontCameraStream,
  setBackCameraStream,
  setBoardCameraStream,
  setVideoAnalyticsDevice,
  setVideoAnalyticsLoading,
  setVideoAnalyticsActive,
  setProcessingMode,
  setAudioStatus,
  setVideoStatus,
  setHasUploadedVideoFiles,
  setMonitoringActive,
  setMonitoringPaused,
  setUploadedVideoFiles,
} from '../../redux/slices/uiSlice';
import { resetTranscript } from '../../redux/slices/transcriptSlice';
import { resetSummary } from '../../redux/slices/summarySlice';
import { clearMindmap } from '../../redux/slices/mindmapSlice';
import { resetMediaValidation } from '../../redux/slices/mediaValidationSlice';
import { markSessionStart } from '../../redux/slices/resourceSlice';
import { useTranslation } from 'react-i18next';
import type { FeatureGuard } from '../../utils/featureGuards';
import { collectPipelineErrors } from '../../utils/pipelineErrors';

interface UploadFilesModalProps {
  isOpen: boolean;
  onClose: () => void;
  featureGuard: FeatureGuard;
}

const UploadFilesModal: React.FC<UploadFilesModalProps> = ({ isOpen, onClose, featureGuard }) => {
  const [audioFile, setAudioFile] = useState<File | null>(null);
  const [frontCameraPath, setFrontCameraPath] = useState<File | null>(null);
  const [rearCameraPath, setRearCameraPath] = useState<File | null>(null);
  const [boardCameraPath, setBoardCameraPath] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  // The same fact as `loading`, kept where handleApply can trust it. `disabled`
  // on the button is not enough on its own: two clicks landing in one render
  // both read `loading` from that render's closure, and a second run here means
  // a second session and a second audio upload.
  const loadingRef = useRef(false);
  /** The stage in flight, or the closing summary once it has finished. */
  const [notification, setNotification] = useState('');
  const { t } = useTranslation();
  const dispatch = useAppDispatch();

  // Check if video_analytics feature is enabled
  const hasVideoAnalyticsFeature = featureGuard.hasFeature('video_analytics');

  // Check if any audio-related features are enabled
  const hasAudioFeatures = featureGuard.hasFeature('asr') ||
    featureGuard.hasFeature('summary') ||
    featureGuard.hasFeature('mindmap') ||
    featureGuard.hasFeature('topic_segmentation');


  const handleFileSelect = (
    setter: React.Dispatch<React.SetStateAction<File | null>>,
    accept: string
  ) => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = accept;
    input.onchange = (e: Event) => {
      const target = e.target as HTMLInputElement;
      if (target.files && target.files[0]) {
        const file = target.files[0];
        const fileName = file.name.toLowerCase();
        let isValidFile = false;
        if (accept === '.wav,.mp3,.m4a') {
          isValidFile = fileName.endsWith('.wav') || fileName.endsWith('.mp3') || fileName.endsWith('.m4a');
        } else if (accept === '.mp4') {
          isValidFile = fileName.endsWith('.mp4');
        } else {
          isValidFile = true;
        }

        if (isValidFile) {
          setter(file);
          console.log('Selected file:', file);
          setError(null);
        } else {
          setter(null);
          const expectedTypes = accept.replace(/\./g, '').replace(/,/g, ', ');
          setError(t('uploadFiles.invalidFileType', { types: expectedTypes }));
        }
      } else {
        setter(null);
        console.log('No file selected');
      }
    };
    input.click();
  };

  /**
   * The reason behind a thrown error, as shown to the user. The backend reports
   * it in the response body (`detail` / `message`), which the API layer re-throws
   * as the Error message. Returns '' when there is nothing usable to show.
   */
  const errorReason = (err: unknown): string => {
    const detail = err instanceof Error ? err.message.trim() : typeof err === 'string' ? err.trim() : '';
    return detail === BACKEND_UNAVAILABLE_MESSAGE ? t('uploadFiles.backendUnavailable') : detail;
  };

  // Failure of the whole upload flow: the backend reason if there is one,
  // otherwise the generic retry prompt.
  const describeError = (err: unknown): string => {
    const reason = errorReason(err);
    return reason
      ? t('uploadFiles.processingFailedDetail', { detail: reason })
      : t('uploadFiles.processingFailed');
  };

  /**
   * Start the video pipelines, reporting both whether anything streams and why
   * the rest did not — the endpoint reports per-pipeline failures with HTTP 200,
   * so they have to be read out of the body (see utils/pipelineErrors).
   */
  const startVideoAnalyticsWithSession = async (
    sessionId: string,
    pipelines: any[]
  ): Promise<{ started: boolean; errors: string[] }> => {
    if (pipelines.length === 0) {
      console.log('📹 No valid video pipelines found, skipping video analytics');
      dispatch(setVideoAnalyticsLoading(false));
      dispatch(setVideoAnalyticsActive(false));
      dispatch(setVideoStatus('no-config'));
      return { started: false, errors: [] };
    }

    const errors: string[] = [];

    try {
      console.log('🎬 Starting video analytics with session ID:', sessionId);
      console.log('🎬 Pipelines to send:', pipelines);
      dispatch(startStream());
      dispatch(setVideoAnalyticsLoading(true));
      dispatch(setVideoStatus('starting')); // This will change from 'processed' to 'starting'

      const videoResponse = await startVideoAnalyticsPipeline(pipelines, sessionId);
      startPipelineMonitoring(sessionId);
      let hasSuccessfulStreams = false;

      videoResponse.results.forEach((result: any) => {
        console.log('Processing result:', result);
        if (result.status === "success" && result.stream_url) {
          hasSuccessfulStreams = true;
          // One device for all three pipelines (config.yaml's
          // models.va.device) -- every successful result carries the same
          // value, so just take it from whichever arrives.
          dispatch(setVideoAnalyticsDevice(result.device ?? null));
          switch (result.pipeline_name) {
            case 'front':
              dispatch(setFrontCameraStream(result.stream_url));
              break;
            case 'back':
              dispatch(setBackCameraStream(result.stream_url));
              break;
            case 'content':
              dispatch(setBoardCameraStream(result.stream_url));
              break;
          }
        } else if (result.status === "error") {
          console.error(`❌ Error with ${result.pipeline_name}:`, result.error);
        }
      });

      errors.push(...collectPipelineErrors(videoResponse.results, t, t('uploadFiles.videoAnalyticsFailed')));

      if (hasSuccessfulStreams) {
        dispatch(setActiveStream('all'));
        dispatch(setVideoAnalyticsActive(true));
        dispatch(setVideoStatus('streaming')); // Only set to streaming when actually streaming
      } else {
        dispatch(setVideoStatus('failed'));
      }

      dispatch(setVideoAnalyticsLoading(false));
      return { started: hasSuccessfulStreams, errors };

    } catch (videoError) {
      console.error('❌ Failed to start video analytics:', videoError);
      dispatch(setVideoAnalyticsLoading(false));
      dispatch(setVideoAnalyticsActive(false));
      dispatch(setVideoStatus('failed'));
      errors.push(errorReason(videoError) || t('uploadFiles.videoAnalyticsFailed'));
      return { started: false, errors };
    }
  };

  const getSuccessNotification = (hasAudio: boolean, hasVideo: boolean, videoStarted: boolean) => {
    const audioSuccess = hasAudio;
    const videoSuccess = hasVideo && videoStarted;

    if (audioSuccess && videoSuccess) {
      return t('uploadFiles.transcriptionAndVideoStarted');
    } else if (audioSuccess && !videoSuccess && hasVideo) {
      return t('uploadFiles.transcriptionStartedVideoFailed');
    } else if (audioSuccess && !hasVideo) {
      return t('uploadFiles.transcriptionStarted');
    } else if (!audioSuccess && videoSuccess) {
      return t('uploadFiles.videoAnalyticsStarted');
    } else if (!audioSuccess && !videoSuccess && hasVideo) {
      return t('uploadFiles.videoAnalyticsFailed');
    } else {
      return t('uploadFiles.noProcessingStarted');
    }
  };

  const handleApply = async () => {
    if (loadingRef.current) return;
    const hasAudioFile = audioFile !== null;
    const hasVideoFiles = frontCameraPath !== null || rearCameraPath !== null || boardCameraPath !== null;

    if (!hasAudioFile && !hasVideoFiles) {
      setError(t('uploadFiles.fileRequired'));
      return;
    }


    setNotification(t('uploadFiles.startingProcessing'));
    dispatch(resetFlow());  // Reset flow FIRST
    dispatch(resetTranscript());
    dispatch(resetSummary());
    dispatch(clearMindmap());
    dispatch(resetMediaValidation());  // Reset media validation state
    dispatch(startProcessing());

    // Set uploaded video files AFTER reset to preserve them
    console.log('🎥 Setting uploaded video files in Redux:', {
      front: frontCameraPath ? frontCameraPath.name : 'null',
      back: rearCameraPath ? rearCameraPath.name : 'null',
      board: boardCameraPath ? boardCameraPath.name : 'null'
    });

    dispatch(setUploadedVideoFiles({
      front: frontCameraPath,
      back: rearCameraPath,
      board: boardCameraPath,
    }));

    if (hasAudioFile) {
      dispatch(setAudioStatus('processing'));
      console.log('🎯 Audio status set to processing - will show "Analyzing audio..."');
    } else {
      dispatch(setAudioStatus('no-devices'));
      console.log('🎯 Audio status set to ready - no audio file selected');
    }

    loadingRef.current = true;
    setLoading(true);
    setError(null);

    try {
      setNotification(t('uploadFiles.creatingSession'));
      const sessionResponse = await createSession();
      const sessionId = sessionResponse.sessionId;
      console.log('✅ Session created:', sessionId);
      dispatch(setSessionId(sessionId));
      // Session-id now created: start showing utilization on the dashboard.
      dispatch(markSessionStart());
      dispatch(setMonitoringPaused(false));
      dispatch(setMonitoringActive(true));

      if (hasAudioFile) {
        setNotification(t('uploadFiles.uploadingAudio'));
        const audioResponse = await uploadAudio(audioFile);
        dispatch(setUploadedAudioPath(audioResponse.path));
        console.log('✅ Audio uploaded successfully:', audioResponse);

        // Extract and store audio duration
        try {
          console.log('🔊 Extracting audio duration from file:', audioFile.name);
          await storeAudioDuration(sessionId, audioFile);
          console.log('✅ Audio duration stored successfully');
        } catch (durationError) {
          console.error('⚠️ Failed to store audio duration:', durationError);
        }

        dispatch(setProcessingMode('audio'));
      } else {
        console.log('📝 No audio file provided, skipping audio upload');
        dispatch(setProcessingMode('video-only'));
      }

      console.log('🎥 Video files uploaded:', {
        frontCameraPath: frontCameraPath ? `File: ${frontCameraPath.name}` : 'null',
        rearCameraPath: rearCameraPath ? `File: ${rearCameraPath.name}` : 'null',
        boardCameraPath: boardCameraPath ? `File: ${boardCameraPath.name}` : 'null'
      });

      // Upload each selected clip to the backend, which persists it into the
      // shared storage volume and returns a container-absolute path the
      // video-analytics side-car can read.
      let frontFullPath = "";
      let rearFullPath = "";
      let boardFullPath = "";
      if (hasVideoFiles && hasVideoAnalyticsFeature) {
        setNotification(t('uploadFiles.startingVideo'));
        if (frontCameraPath) frontFullPath = (await uploadVideo(frontCameraPath, 'front', sessionId)).path;
        if (rearCameraPath) rearFullPath = (await uploadVideo(rearCameraPath, 'back', sessionId)).path;
        if (boardCameraPath) boardFullPath = (await uploadVideo(boardCameraPath, 'content', sessionId)).path;
      }

      console.log('📹 Uploaded video source paths:', {
        front: frontFullPath,
        rear: rearFullPath,
        board: boardFullPath,
      });

      const allPipelines = [
        {
          pipeline_name: 'front',
          source: frontFullPath
        },
        {
          pipeline_name: 'back',
          source: rearFullPath
        },
        {
          pipeline_name: 'content',
          source: boardFullPath
        },
      ];

      const validPipelines = allPipelines.filter(pipeline =>
        pipeline.source && pipeline.source.trim() !== ''
      );

      const hasValidVideo = validPipelines.length > 0;
      console.log('🎯 Has valid video files:', hasValidVideo);

      // Only process videos if video_analytics feature is enabled
      if (!hasVideoAnalyticsFeature && hasValidVideo) {
        console.warn('⚠️ Video files selected but video_analytics feature is disabled. Skipping video processing.');
        dispatch(setVideoStatus('no-config'));
        dispatch(setHasUploadedVideoFiles(false));
      } else {
        dispatch(setHasUploadedVideoFiles(hasValidVideo));

        if (hasValidVideo && hasVideoAnalyticsFeature) {
          console.log('🎥 Setting uploaded video files in Redux (second time, inside video block):', {
            front: frontCameraPath ? frontCameraPath.name : 'null',
            back: rearCameraPath ? rearCameraPath.name : 'null',
            board: boardCameraPath ? boardCameraPath.name : 'null'
          });

          dispatch(setUploadedVideoFiles({
            front: frontCameraPath,
            back: rearCameraPath,
            board: boardCameraPath,
          }));

          dispatch(setHasUploadedVideoFiles(true));

          if (rearCameraPath)
            dispatch(setActiveStream('back'));

          else if (boardCameraPath)
            dispatch(setActiveStream('content'));

          else if (frontCameraPath)
            dispatch(setActiveStream('front'));
        }
        else {
          dispatch(setVideoStatus('no-config'));
        }
      }

      let videoAnalyticsStarted = false;
      let videoErrors: string[] = [];
      if (hasValidVideo && hasVideoAnalyticsFeature) {
        setNotification(t('uploadFiles.startingVideo'));
        ({ started: videoAnalyticsStarted, errors: videoErrors } =
          await startVideoAnalyticsWithSession(sessionId, validPipelines));
        if (videoAnalyticsStarted) {
          console.log('✅ Video analytics started successfully');
        } else {
          console.warn('⚠️ Video analytics failed to start');
          dispatch(setVideoStatus('failed'));
        }
      } else if (!hasVideoAnalyticsFeature && hasValidVideo) {
        console.log('📹 Video files present but feature disabled, skipping video analytics');
      } else {
        console.log('📹 No valid video files provided, skipping video analytics');
      }

      const finalNotification = getSuccessNotification(hasAudioFile, hasValidVideo, videoAnalyticsStarted);

      console.log(finalNotification)
      setNotification(finalNotification);

      console.log('✅ Processing summary:', {
        audioFile: hasAudioFile,
        videoFiles: hasValidVideo,
        videoAnalyticsStarted,
        finalMessage: finalNotification
      });

      loadingRef.current = false;
      setLoading(false);

      // Show the reasons and keep the modal open. Whatever did start
      // (transcription, other pipelines) keeps running in the background.
      if (videoErrors.length > 0) {
        setError(t('errors.videoPipelineFailed', { details: videoErrors.join('\n') }));
        return;
      }

      onClose();
    } catch (err) {
      console.error('❌ Failed during processing:', err);
      setError(describeError(err));
      setNotification('');
      dispatch(processingFailed());
      loadingRef.current = false;
      setLoading(false);
    }
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} closeOnOverlayClick={false}>
      <div className="upload-files-modal">
        <h2>{t('uploadFiles.title')}</h2>
        <hr className="modal-title-line" />
        <div className="modal-body">

          {/* Audio upload section - only show if audio features are enabled */}
          {hasAudioFeatures ? (
            <div className="modal-input-group modal-title fw-semibold">
              <label>{t('uploadFiles.audioFileLabel')}</label>
              <div className="file-input-wrapper">
                <input
                  type="text"
                  value={audioFile?.name || ''}
                  readOnly
                  placeholder={t('uploadFiles.selectAudioFile')}
                />
                <img
                  src={folderIcon}
                  alt={t('uploadFiles.chooseFile')}
                  className="folder-icon"
                  onClick={() => handleFileSelect(setAudioFile, '.wav,.mp3,.m4a')}
                />
              </div>
            </div>
          ) : (
            <div className="modal-info-message" style={{ marginBottom: '1rem', padding: '0.75rem', backgroundColor: '#f0f0f0', borderRadius: '4px', color: '#666' }}>
              {t('uploadFiles.audioFeaturesDisabled')}
            </div>
          )}

          {/* Video upload sections - only show if video_analytics is enabled */}
          {hasVideoAnalyticsFeature ? (
            <>
              <div className="modal-input-group">
                <label>{t('uploadFiles.frontCameraFile')}</label>
                <div className="file-input-wrapper">
                  <input
                    type="text"
                    value={frontCameraPath?.name || ''}
                    readOnly
                    placeholder={t('uploadFiles.selectFrontCameraFile')}
                    title={frontCameraPath?.name || ''}
                  />
                  <img
                    src={folderIcon}
                    alt={t('uploadFiles.chooseFile')}
                    className="folder-icon"
                    onClick={() => handleFileSelect(setFrontCameraPath, '.mp4')}
                  />
                </div>
              </div>

              <div className="modal-input-group">
                <label>{t('uploadFiles.backCameraFile')}</label>
                <div className="file-input-wrapper">
                  <input
                    type="text"
                    value={rearCameraPath?.name || ''}
                    readOnly
                    placeholder={t('uploadFiles.selectBackCameraFile')}
                    title={rearCameraPath?.name || ''}
                  />
                  <img
                    src={folderIcon}
                    alt={t('uploadFiles.chooseFile')}
                    className="folder-icon"
                    onClick={() => handleFileSelect(setRearCameraPath, '.mp4')}
                  />
                </div>
              </div>

              <div className="modal-input-group">
                <label>{t('uploadFiles.boardCameraFile')}</label>
                <div className="file-input-wrapper">
                  <input
                    type="text"
                    value={boardCameraPath?.name || ''}
                    readOnly
                    placeholder={t('uploadFiles.selectBoardCameraFile')}
                    title={boardCameraPath?.name || ''}
                  />
                  <img
                    src={folderIcon}
                    alt={t('uploadFiles.chooseFile')}
                    className="folder-icon"
                    onClick={() => handleFileSelect(setBoardCameraPath, '.mp4')}
                  />
                </div>
              </div>
            </>
          ) : (
            <div className="modal-info-message" style={{ marginTop: '1rem', padding: '0.75rem', backgroundColor: '#f0f0f0', borderRadius: '4px', color: '#666' }}>
              {t('uploadFiles.videoAnalyticsDisabled')}
            </div>
          )}
          {error && <div className="error-message">{error}</div>}
          {/* Same row for both, because it is the same line of text moving on:
              the spinner is what distinguishes a stage still running from the
              summary left behind once it finished. */}
          {notification && (
            <div className="modal-progress" role="status">
              {loading && <span className="modal-spinner" aria-hidden="true" />}
              {notification}
            </div>
          )}
        </div>
        <div className="modal-actions">
          <button
            onClick={handleApply}
            className="apply-button"
            disabled={(!audioFile && !frontCameraPath && !rearCameraPath && !boardCameraPath) || loading}
          >
            {loading ? t('uploadFiles.processing') : t('uploadFiles.applyAndStart')}
          </button>
        </div>
      </div>
    </Modal>
  );
};

export default UploadFilesModal;