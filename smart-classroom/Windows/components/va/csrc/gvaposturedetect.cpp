/*******************************************************************************
 * Copyright (C) 2025 Intel Corporation
 *
 * SPDX-License-Identifier: MIT
 ******************************************************************************/

#include "gvaposturedetect.h"
#include "extended_roi.h"

#include <algorithm>
#include <cmath>
#include <deque>
#include <fstream>
#include <memory>
#include <optional>
#include <string>
#include <unordered_map>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

GST_DEBUG_CATEGORY(gst_gvaposturedetect_debug);
#define GST_CAT_DEFAULT gst_gvaposturedetect_debug

// Function declarations
static void gst_posture_detect_class_init(GstPostureDetectClass *klass);
static void gst_posture_detect_init(GstPostureDetect *self);
static void gst_posture_detect_finalize(GObject *object);
static gboolean gst_posture_detect_set_caps(GstBaseTransform *trans, GstCaps *incaps, GstCaps *outcaps);
static GstFlowReturn gst_posture_detect_transform_ip(GstBaseTransform *trans, GstBuffer *buf);

// Data structures
struct Keypoint {
    float x, y;
    float confidence;
};

struct Person {
    std::vector<Keypoint> keypoints; // frame pixel coordinates
    float confidence;
    float box_width;
    float box_height;
    float box_center_x;
    float box_bottom;
};

struct HeldPosture {
    std::string posture;
    guint last_seen_frame;
    // Keypoint tensor of the last inferred frame (ROI-relative coordinates),
    // re-attached to the tracked box so gvawatermark keeps drawing the skeleton.
    std::shared_ptr<GstStructure> keypoints;
};
using HeldPostures = std::unordered_map<int32_t, HeldPosture>;

// A held posture is dropped once its object id has not been seen for this many
// frames (the tracker has lost it).
static const guint HELD_POSTURE_MAX_AGE = 150;

// Raised-hand rule thresholds, in shoulder widths / cosine to straight up.
// Tuned on a manually annotated classroom recording (see detect_posture).
static const float RAISE_WRIST_ABOVE_SHOULDER = 0.4f;
static const float RAISE_FOREARM_UP_COS = 0.7f; // forearm within ~45 deg of vertical
static const float RAISE_WRIST_OFF_FACE = 0.6f;

// Seat-baseline stand rule: a student who stands up lifts the head well above
// where heads at that seat (same box centre x and box bottom, in current box
// sizes) were over the past SEAT_HISTORY_SEC, leaving out the last
// SEAT_GAP_SEC that may already show the person rising. Rise is in shoulder
// widths. Validated on three manually annotated classroom recordings.
static const double SEAT_HISTORY_SEC = 30.0;
static const double SEAT_GAP_SEC = 3.0;
static const float SEAT_MATCH_BOX = 0.5f; // max centre x / bottom offset, in box widths / heights
static const size_t SEAT_MIN_SAMPLES = 10;
static const float SEAT_STAND_RISE = 0.8f;

struct SeatSample {
    double time; // seconds, buffer PTS
    float center_x;
    float bottom;
    float head_y;
};
struct SeatHistory {
    std::deque<SeatSample> samples; // inferred frames only, oldest first
    double last_time = -1.0;
};

static std::string detect_posture(const Person &person, std::optional<float> rise);

// YOLO pose keypoint indices
enum YoloKeypoints {
    NOSE = 0,
    LEFT_EYE = 1,
    RIGHT_EYE = 2,
    LEFT_EAR = 3,
    RIGHT_EAR = 4,
    LEFT_SHOULDER = 5,
    RIGHT_SHOULDER = 6,
    LEFT_ELBOW = 7,
    RIGHT_ELBOW = 8,
    LEFT_WRIST = 9,
    RIGHT_WRIST = 10,
    LEFT_HIP = 11,
    RIGHT_HIP = 12,
    LEFT_KNEE = 13,
    RIGHT_KNEE = 14,
    LEFT_ANKLE = 15,
    RIGHT_ANKLE = 16
};

// Length unit of a person: the shoulder width, or a box-width estimate when
// the shoulders are not both visible or are seen edge-on.
static float body_unit(const Person &person) {
    const auto &ls = person.keypoints[YoloKeypoints::LEFT_SHOULDER];
    const auto &rs = person.keypoints[YoloKeypoints::RIGHT_SHOULDER];
    if (ls.confidence > 0.5 && rs.confidence > 0.5) {
        float sw = std::hypot(ls.x - rs.x, ls.y - rs.y);
        if (sw > 0.15f * person.box_width)
            return sw;
    }
    return 0.4f * person.box_width;
}

// Top of the head: the highest visible face keypoint.
static std::optional<float> head_y(const Person &person) {
    std::optional<float> top;
    for (int i = YoloKeypoints::NOSE; i <= YoloKeypoints::RIGHT_EAR; ++i) {
        const auto &kp = person.keypoints[i];
        if (kp.confidence > 0.5 && (!top || kp.y < *top))
            top = kp.y;
    }
    return top;
}

// Head rise over the seat baseline (median head y of the history samples at
// this box position), in body units; none without a head or enough history.
static std::optional<float> seat_rise(const SeatHistory &history, const Person &person, double now) {
    auto head = head_y(person);
    float unit = body_unit(person);
    if (!head || unit <= 0)
        return std::nullopt;
    std::vector<float> heads;
    for (const auto &s : history.samples) {
        if (s.time < now - SEAT_HISTORY_SEC || s.time > now - SEAT_GAP_SEC)
            continue;
        if (std::fabs(s.center_x - person.box_center_x) <= SEAT_MATCH_BOX * person.box_width &&
            std::fabs(s.bottom - person.box_bottom) <= SEAT_MATCH_BOX * person.box_height)
            heads.push_back(s.head_y);
    }
    if (heads.size() < SEAT_MIN_SAMPLES)
        return std::nullopt;
    size_t mid = heads.size() / 2;
    std::nth_element(heads.begin(), heads.begin() + mid, heads.end());
    float baseline = heads[mid];
    if (heads.size() % 2 == 0)
        baseline = (baseline + *std::max_element(heads.begin(), heads.begin() + mid)) / 2;
    return (baseline - *head) / unit;
}

// Posture detection function; rise is the seat_rise() of the person, none to
// judge standing from the legs alone.
static std::string detect_posture(const Person &person, std::optional<float> rise) {
    const auto &kpts = person.keypoints;

    assert(kpts.size() == 17);

    // Helper function to calculate angle with vertical vector [0, 1]
    auto angle = [](float vx, float vy) -> float {
        float norm = std::sqrt(vx * vx + vy * vy);
        if (norm == 0) {
            return 0.0f;
        }
        // Dot product with [0, 1] is just vy
        float cos_angle = vy / norm;
        // Clamp to [-1, 1] to avoid numerical issues
        cos_angle = std::max(-1.0f, std::min(1.0f, cos_angle));
        return std::acos(cos_angle) * 180.0f / M_PI; // Convert to degrees
    };

    std::string posture = "sit"; // Default posture
    bool raise_up = false;

    try {
        // Get keypoints using YOLO indices
        auto left_hip = kpts[YoloKeypoints::LEFT_HIP];
        auto right_hip = kpts[YoloKeypoints::RIGHT_HIP];
        auto left_knee = kpts[YoloKeypoints::LEFT_KNEE];
        auto right_knee = kpts[YoloKeypoints::RIGHT_KNEE];

        // Standing: the head well above the seat baseline. Without a baseline
        // (no head, or a place nobody has sat at lately), a thigh near vertical.
        if (rise) {
            posture = *rise > SEAT_STAND_RISE ? "stand" : "sit";
        }
        // Check if both legs are visible (confidence > 0.5)
        else if (left_hip.confidence > 0.5 && right_hip.confidence > 0.5 && left_knee.confidence > 0.5 &&
                 right_knee.confidence > 0.5) {

            // Calculate vectors from hip to knee
            float left_vec_x = left_knee.x - left_hip.x;
            float left_vec_y = left_knee.y - left_hip.y;
            float right_vec_x = right_knee.x - right_hip.x;
            float right_vec_y = right_knee.y - right_hip.y;

            // Calculate angles with vertical
            float left_angle = angle(left_vec_x, left_vec_y);
            float right_angle = angle(right_vec_x, right_vec_y);

            if (left_angle < 30.0f && right_angle < 30.0f) {
                posture = "stand";
            }
        }
        // Check if only left leg is visible
        else if (left_hip.confidence > 0.5 && left_knee.confidence > 0.5) {
            float left_vec_x = left_knee.x - left_hip.x;
            float left_vec_y = left_knee.y - left_hip.y;
            float left_angle = angle(left_vec_x, left_vec_y);

            if (left_angle < 30.0f) {
                posture = "stand";
            }
        }
        // Check if only right leg is visible
        else if (right_hip.confidence > 0.5 && right_knee.confidence > 0.5) {
            float right_vec_x = right_knee.x - right_hip.x;
            float right_vec_y = right_knee.y - right_hip.y;
            float right_angle = angle(right_vec_x, right_vec_y);

            if (right_angle < 30.0f) {
                posture = "stand";
            }
        }
        // Check if only hips are visible (no legs) - assume sitting
        else if (left_hip.confidence > 0.5 && right_hip.confidence > 0.5) {
            posture = "sit";
        }

        // Raised hand: the hand is up beside the head, not resting on the face.
        // "Wrist above shoulder" alone also fires on a chin or head propped on
        // a hand; that pose has the forearm sloping to the face and the wrist
        // at the nose. Lengths are in shoulder widths (box widths when the
        // shoulders are not both visible or are seen edge-on).
        const auto &nose = kpts[YoloKeypoints::NOSE];
        float unit = body_unit(person);
        const int arms[2][3] = {{YoloKeypoints::LEFT_WRIST, YoloKeypoints::LEFT_ELBOW, YoloKeypoints::LEFT_SHOULDER},
                                {YoloKeypoints::RIGHT_WRIST, YoloKeypoints::RIGHT_ELBOW, YoloKeypoints::RIGHT_SHOULDER}};
        for (const auto &arm : arms) {
            const auto &wrist = kpts[arm[0]];
            const auto &elbow = kpts[arm[1]];
            const auto &shoulder = kpts[arm[2]];
            if (unit <= 0 || wrist.confidence <= 0.5 || elbow.confidence <= 0.5 || shoulder.confidence <= 0.5)
                continue;
            if ((shoulder.y - wrist.y) / unit <= RAISE_WRIST_ABOVE_SHOULDER)
                continue;
            float fx = wrist.x - elbow.x, fy = wrist.y - elbow.y;
            float fn = std::hypot(fx, fy);
            if (fn == 0 || -fy / fn < RAISE_FOREARM_UP_COS)
                continue;
            if (nose.confidence > 0.5 && std::fabs(wrist.x - nose.x) / unit < RAISE_WRIST_OFF_FACE)
                continue;
            raise_up = true;
            break;
        }
    } catch (const std::exception &e) {
        GST_WARNING("Error detecting posture: %s", e.what());
        posture = "sit";
    }

    // Append raising_hand if detected
    if (raise_up) {
        posture += "_raise_up";
    }

    return posture;
}

// GType registration
G_DEFINE_TYPE(GstPostureDetect, gst_posture_detect, GST_TYPE_BASE_TRANSFORM)

static void gst_posture_detect_class_init(GstPostureDetectClass *klass) {
    GObjectClass *gobject_class = G_OBJECT_CLASS(klass);
    GstElementClass *element_class = GST_ELEMENT_CLASS(klass);
    GstBaseTransformClass *base_transform_class = GST_BASE_TRANSFORM_CLASS(klass);

    gobject_class->finalize = gst_posture_detect_finalize;

    gst_element_class_set_static_metadata(element_class, "Posture Detection", "Filter/Analyzer/Video",
                                          "Analyzes YOLO pose keypoints to determine sitting/standing postures",
                                          "Intel Corporation");

    gst_element_class_add_pad_template(
        element_class, gst_pad_template_new("src", GST_PAD_SRC, GST_PAD_ALWAYS, gst_caps_from_string("ANY")));

    gst_element_class_add_pad_template(
        element_class, gst_pad_template_new("sink", GST_PAD_SINK, GST_PAD_ALWAYS, gst_caps_from_string("ANY")));

    base_transform_class->set_caps = GST_DEBUG_FUNCPTR(gst_posture_detect_set_caps);
    base_transform_class->transform_ip = GST_DEBUG_FUNCPTR(gst_posture_detect_transform_ip);

    GST_DEBUG_CATEGORY_INIT(gst_gvaposturedetect_debug, "gvaposturedetect", 0, "Posture Detection");
}

static void gst_posture_detect_init(GstPostureDetect *self) {
    self->frame_count = 0;
    self->info = nullptr;
    self->held_postures = new HeldPostures();
    self->seat_history = new SeatHistory();
}

static void gst_posture_detect_finalize(GObject *object) {
    GstPostureDetect *self = GST_POSTURE_DETECT(object);

    delete static_cast<HeldPostures *>(self->held_postures);
    self->held_postures = nullptr;
    delete static_cast<SeatHistory *>(self->seat_history);
    self->seat_history = nullptr;

    if (self->info) {
        gst_video_info_free(self->info);
        self->info = nullptr;
    }

    G_OBJECT_CLASS(gst_posture_detect_parent_class)->finalize(object);
}

static gboolean gst_posture_detect_set_caps(GstBaseTransform *trans, GstCaps *incaps, GstCaps *outcaps) {
    GstPostureDetect *self = GST_POSTURE_DETECT(trans);

    // Create or update video info from input caps
    if (!self->info) {
        self->info = gst_video_info_new();
    }

    if (!gst_video_info_from_caps(self->info, incaps)) {
        GST_ERROR_OBJECT(self, "Failed to parse video info from input caps");
        return FALSE;
    }

    return TRUE;
}

static GstFlowReturn gst_posture_detect_transform_ip(GstBaseTransform *trans, GstBuffer *buf) {
    GstPostureDetect *self = GST_POSTURE_DETECT(trans);

    self->frame_count++;

    GVA::VideoFrame video_frame(buf, self->info);
    auto regions = video_frame.regions();
    auto &held = *static_cast<HeldPostures *>(self->held_postures);
    // Keypoints only come with an inferred frame; boxes a tracker carried over
    // to a frame gvadetect skipped have none.
    bool inferred = regions.empty();

    // Seat history is kept in stream time; a timestamp going backwards means a
    // new stream, whose seats are unrelated.
    auto &seats = *static_cast<SeatHistory *>(self->seat_history);
    const GstClockTime pts = GST_BUFFER_PTS(buf);
    const bool has_time = GST_CLOCK_TIME_IS_VALID(pts);
    const double now = has_time ? static_cast<double>(pts) / GST_SECOND : 0.0;
    if (has_time && now < seats.last_time)
        seats.samples.clear();
    std::vector<SeatSample> new_samples;

    // Perform posture detection and count postures
    int stand_count = 0;
    int sit_count = 0;
    int raise_up_count = 0;

    for (auto &roi : regions) {
        Person person;
        person.confidence = roi.confidence();

        // Extract keypoints from tensors attached to this ROI
        bool has_keypoints = false;
        GstStructure *keypoints_structure = nullptr;
        for (GList *l = roi.get_params(); l; l = g_list_next(l)) {
            GstStructure *s = GST_STRUCTURE(l->data);
            GVA::Tensor tensor(s);
            if (!tensor.is_detection() && tensor.type() == GVA::GST_ANALYTICS_KEYPOINTS_2_TENSOR) {
                keypoints_structure = s;
                if (tensor.format() != GST_ANALYTICS_KEYPOINT_BODY_POSE_COCO_17) {
                    GST_ELEMENT_ERROR(self, STREAM, FAILED,
                                      ("Unsupported keypoint format: %s", tensor.format().c_str()), (NULL));
                    return GST_FLOW_ERROR;
                }
                auto keypoints_data = tensor.data<float>();
                auto confidence = tensor.confidences();
                auto dims = tensor.dims();

                if (dims.size() >= 2) {
                    size_t points_num = dims[0];
                    size_t point_dimension = dims[1];
                    auto roi_rect = roi.rect();

                    person.keypoints.resize(points_num);
                    person.box_width = static_cast<float>(roi_rect.w);
                    person.box_height = static_cast<float>(roi_rect.h);
                    person.box_center_x = roi_rect.x + 0.5f * roi_rect.w;
                    person.box_bottom = static_cast<float>(roi_rect.y + roi_rect.h);
                    for (size_t i = 0; i < points_num; ++i) {
                        // Tensor keypoints are relative to the ROI; posture rules
                        // measure lengths and angles, so use frame pixels.
                        auto x = roi_rect.x + keypoints_data[point_dimension * i] * roi_rect.w;
                        auto y = roi_rect.y + keypoints_data[point_dimension * i + 1] * roi_rect.h;
                        person.keypoints[i].x = static_cast<float>(x);
                        person.keypoints[i].y = static_cast<float>(y);
                        person.keypoints[i].confidence = (confidence.size() > i) ? confidence[i] : 1.0f;

                        // Validate keypoint is within ROI bounds
                        float tolerance_x = roi_rect.w * 0.2f;
                        float tolerance_y = roi_rect.h * 0.2f;
                        if (x < roi_rect.x - tolerance_x || x > roi_rect.x + roi_rect.w + tolerance_x ||
                            y < roi_rect.y - tolerance_y || y > roi_rect.y + roi_rect.h + tolerance_y) {
                            // Keypoint is outside ROI bounds, reduce confidence
                            person.keypoints[i].confidence *= 0.1f;
                        }
                    }
                    has_keypoints = true;
                }
                break;
            }
        }

        // Detect posture and update ROI label; a tracked box without keypoints
        // keeps the posture its object id had at the last inferred frame
        std::string posture = "no_keypoints";
        const int32_t object_id = roi.object_id();
        if (has_keypoints) {
            inferred = true;
            std::optional<float> rise;
            if (has_time) {
                rise = seat_rise(seats, person, now);
                if (auto head = head_y(person))
                    new_samples.push_back({now, person.box_center_x, person.box_bottom, *head});
            }
            posture = detect_posture(person, rise);
            if (object_id > 0)
                held[object_id] = {posture, self->frame_count,
                                   std::shared_ptr<GstStructure>(gst_structure_copy(keypoints_structure),
                                                                 gst_structure_free)};
        } else if (object_id > 0) {
            auto it = held.find(object_id);
            if (it != held.end()) {
                posture = it->second.posture;
                it->second.last_seen_frame = self->frame_count;
                // gvatrack creates the boxes of skipped frames without keypoints;
                // the ROI takes ownership of the copy
                if (it->second.keypoints)
                    roi.add_tensor(GVA::Tensor(gst_structure_copy(it->second.keypoints.get())));
            }
        }
        if (posture != "no_keypoints") {
            roi._meta()->roi_type = g_quark_from_string(posture.c_str());

            GstAnalyticsODMtdData *data = get_mtd_data_from_roi(roi);
            data->object_type = g_quark_from_string(posture.c_str());
        }
        // The tracker id served only to hold the posture. Clear it so ids
        // downstream keep meaning what gvareid assigns (standing students),
        // as they did before a tracker was added.
        if (object_id > 0)
            roi.set_object_id(0);

        // Update posture counts
        if (posture.starts_with("stand")) {
            stand_count++;
        } else if (posture.starts_with("sit")) {
            sit_count++;
        }
        if (posture.ends_with("raise_up")) {
            raise_up_count++;
        }
    }

    // Add a 0x0 watermark region at top-left corner for posture count display
    std::string count_text = "Sit: " + std::to_string(sit_count) + " | Stand: " + std::to_string(stand_count) +
                             " | Raise Up: " + std::to_string(raise_up_count);
    auto count_region = video_frame.add_region(0, 0, 0, 0, "", 0.0f);
    GstStructure *count_gst_structure = gst_structure_new_empty("count_text");
    GVA::Tensor count_tensor(count_gst_structure);
    count_tensor.set_string("label", count_text);
    count_region.add_tensor(count_tensor);

    // Frame-level marker so statistics can use inferred frames only; published
    // by gvametaconvert as {"posture_frame": {"label_id": 1 (inferred) | 0 (tracked)}}.
    // label_id rather than a label string, which gvawatermark would draw.
    GVA::Tensor frame_tensor = video_frame.add_tensor();
    frame_tensor.set_name("posture_frame");
    frame_tensor.set_int("label_id", inferred ? 1 : 0);

    // This frame joins the history only now, so every person on it was compared
    // against earlier frames alone.
    if (has_time) {
        seats.samples.insert(seats.samples.end(), new_samples.begin(), new_samples.end());
        while (!seats.samples.empty() && seats.samples.front().time < now - SEAT_HISTORY_SEC)
            seats.samples.pop_front();
        seats.last_time = now;
    }

    for (auto it = held.begin(); it != held.end();) {
        if (self->frame_count - it->second.last_seen_frame > HELD_POSTURE_MAX_AGE)
            it = held.erase(it);
        else
            ++it;
    }

    return GST_FLOW_OK;
}
