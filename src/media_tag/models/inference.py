"""Actual model inference primitives. All weights are verified before load; no automatic download."""

import time

from .contracts import ModelFailure
from .native import prepare_native_runtime


def gpu_probe():
    prepare_native_runtime()
    import torch
    if not torch.cuda.is_available():
        raise ModelFailure('cuda_unavailable')
    torch.set_num_threads(1)
    started = time.monotonic()
    torch.cuda.reset_peak_memory_stats()
    # Exercise real kernels, not only enumeration/import; a deterministic product has known values.
    x = torch.arange(256 * 256, device='cuda', dtype=torch.float32).reshape(256, 256) / 65536
    y = x @ x.T
    torch.cuda.synchronize()
    actual = y.cpu()
    expected = x.cpu() @ x.cpu().T
    error = float((actual - expected).abs().max())
    if not torch.isfinite(actual).all() or error > 0.001:
        raise ModelFailure('cuda_numeric_check_failed')
    return {'torch': torch.__version__, 'cuda_runtime': torch.version.cuda, 'device': torch.cuda.get_device_name(),
            'capability': list(torch.cuda.get_device_capability()), 'compiled_arches': torch.cuda.get_arch_list(),
            'matrix_max_abs_error': error, 'elapsed_seconds': time.monotonic() - started,
            'peak_allocated_bytes': torch.cuda.max_memory_allocated()}


class OCRModel:
    def __init__(self, registry):
        prepare_native_runtime()
        paths = registry.verify('ocr')
        from rapidocr import RapidOCR
        from rapidocr.utils.typings import OCRVersion, ModelType
        self.engine = RapidOCR(params={
            'Det.ocr_version': OCRVersion.PPOCRV5, 'Rec.ocr_version': OCRVersion.PPOCRV5, 'Cls.ocr_version': OCRVersion.PPOCRV5,
            'Det.model_type': ModelType.MOBILE, 'Rec.model_type': ModelType.MOBILE, 'Cls.model_type': ModelType.MOBILE,
            'Det.model_path': str(paths['ch_PP-OCRv5_det_mobile.onnx']),
            'Rec.model_path': str(paths['ch_PP-OCRv5_rec_mobile.onnx']),
            'Cls.model_path': str(paths['ch_PP-LCNet_x0_25_textline_ori_cls_mobile.onnx']),
            'Cls.cls_image_shape': [3, 80, 160], 'Global.log_level': 'critical',
            'Det.mean': [0.485, 0.456, 0.406], 'Det.std': [0.229, 0.224, 0.225], 'Det.limit_type': 'max',
            'EngineConfig.onnxruntime.intra_op_num_threads': 1, 'EngineConfig.onnxruntime.inter_op_num_threads': 1,
            'EngineConfig.onnxruntime.use_cuda': False, 'Global.max_side_len': 2048,
        })

    def infer(self, image, options):
        import numpy as np
        output = self.engine(np.asarray(image.convert('RGB'))[:, :, ::-1].copy(), text_score=options.text_score)
        lines = []
        for box, text, score in zip(output.boxes if output.boxes is not None else [], output.txts or (), output.scores or ()):
            if not np.isfinite(box).all() or not np.isfinite(score):
                raise ModelFailure('model_nonfinite_output')
            lines.append({'text': str(text), 'quadrilateral': box.astype(float).tolist(), 'recognition_score': float(score),
                          'score_kind': 'recognizer_score_not_medical_probability'})
        return {'state': 'observed' if lines else 'no_detection_in_sample', 'lines': lines,
                'device': 'cpu', 'language': 'model_supported_mixed_text_not_verified_language_id'}


class FaceModel:
    def __init__(self, registry):
        prepare_native_runtime()
        paths = registry.verify('face')
        import cv2
        cv2.setNumThreads(1)
        self.cv2, self.path = cv2, str(paths['yunet.onnx'])

    def infer(self, image, options):
        import numpy as np
        rgb = np.asarray(image.convert('RGB'))
        height, width = rgb.shape[:2]
        detector = self.cv2.FaceDetectorYN.create(self.path, '', (width, height), options.score_threshold,
            options.nms_threshold, options.max_detections, self.cv2.dnn.DNN_BACKEND_OPENCV, self.cv2.dnn.DNN_TARGET_CPU)
        _, found = detector.detect(rgb[:, :, ::-1].copy())
        faces = []
        for row in found if found is not None else []:
            if not np.isfinite(row).all():
                raise ModelFailure('model_nonfinite_output')
            x, y, w, h = [float(v) for v in row[:4]]
            left, top, right, bottom = max(0.0, x), max(0.0, y), min(float(width), x + w), min(float(height), y + h)
            if right <= left or bottom <= top:
                continue
            faces.append({'box_xyxy': [left, top, right, bottom], 'detection_score': float(row[-1]),
                          'landmarks_xy': row[4:14].reshape(5, 2).astype(float).tolist()})
        return {'state': 'observed' if faces else 'no_detection_in_sample', 'faces': faces,
                'device': 'cpu', 'identity_inference': False, 'redaction_completed': False}


class CLIPModel:
    def __init__(self, registry, *, device='cuda'):
        prepare_native_runtime()
        paths = registry.verify('clip')
        import torch
        from transformers import ChineseCLIPModel, ChineseCLIPProcessor
        if device not in ('cpu', 'cuda') or device == 'cuda' and not torch.cuda.is_available():
            raise ModelFailure('cuda_unavailable')
        torch.set_num_threads(1)
        self.torch, self.device = torch, device
        directory = paths['config.json'].parent
        self.processor = ChineseCLIPProcessor.from_pretrained(directory, local_files_only=True, use_fast=False)
        self.model = ChineseCLIPModel.from_pretrained(directory, local_files_only=True, use_safetensors=False,
            weights_only=True, attn_implementation='eager').eval().to(device)

    def infer(self, image, *, texts=()):
        torch = self.torch
        if self.device == 'cuda':
            torch.cuda.reset_peak_memory_stats()
            torch.cuda.synchronize()
        started = time.monotonic()
        with torch.inference_mode():
            inputs = self.processor(images=image.convert('RGB'), return_tensors='pt')
            image_features = self.model.get_image_features(**{k: v.to(self.device) for k, v in inputs.items()})
            image_features = torch.nn.functional.normalize(image_features.float(), dim=-1)
            scores = []
            if texts:
                tokens = self.processor(text=list(texts), padding=True, truncation=True, max_length=52, return_tensors='pt')
                # Upstream Chinese-CLIP uses the final BERT [CLS] state, without a pooler.
                # Transformers 4.57.6 get_text_features incorrectly reads its absent pooler_output.
                text_output = self.model.text_model(**{k: v.to(self.device) for k, v in tokens.items()}, return_dict=True)
                features = self.model.text_projection(text_output.last_hidden_state[:, 0, :])
                features = torch.nn.functional.normalize(features.float(), dim=-1)
                scores = (image_features @ features.T)[0].cpu().tolist()
            embedding = image_features[0].cpu().tolist()
            if not torch.isfinite(image_features).all() or any(not -1.00001 <= value <= 1.00001 for value in scores):
                raise ModelFailure('model_nonfinite_output')
        if self.device == 'cuda':
            torch.cuda.synchronize()
        return {'embedding': embedding, 'cosine_scores': scores, 'score_kind': 'cosine_not_probability',
                'device': self.device, 'elapsed_seconds': time.monotonic() - started,
                'peak_gpu_allocated_bytes': torch.cuda.max_memory_allocated() if self.device == 'cuda' else None}
