from hashlib import sha256
from math import sqrt
from pathlib import Path
import gc

from PIL import Image
import pytest

from media_tag.models.contracts import FaceOptions, OCROptions, ModelFailure
from media_tag.models.inference import CLIPModel, FaceModel, OCRModel, gpu_probe


def test_actual_ocr_positive_and_blank_are_observations_not_medical_tags(registry, text_image, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError('inference must not download')
    monkeypatch.setattr('urllib.request.urlopen', forbidden)
    model = OCRModel(registry)
    output = model.infer(text_image, OCROptions())
    text = ''.join(line['text'] for line in output['lines'])
    assert '测试' in text and 'ABC' in text and '123' in text
    assert len(output['lines'][0]['quadrilateral']) == 4
    assert model.infer(Image.new('RGB', (320, 240), 'white'), OCROptions())['state'] == 'no_detection_in_sample'


def test_actual_yunet_face_and_blank_do_not_assert_identity_or_privacy(registry):
    path = Path('cache/models/d04/public-face-sample.jpg')
    assert path.is_file(), 'Prepare the pinned public diagnostic sample described in tests/models/README.md'
    assert sha256(path.read_bytes()).hexdigest() == '7de7ed51a1594fff247f4cae2301eceacf5313d6011e37b4a4c8733f7bb72c07'
    model = FaceModel(registry)
    with Image.open(path) as image:
        report = model.infer(image, FaceOptions())
    assert len(report['faces']) == 1 and not report['identity_inference'] and not report['redaction_completed']
    x1, y1, x2, y2 = report['faces'][0]['box_xyxy']
    assert 0 <= x1 < x2 <= 512 and 0 <= y1 < y2 <= 512
    assert model.infer(Image.new('RGB', (320, 240), 'white'), FaceOptions())['state'] == 'no_detection_in_sample'


def test_actual_cuda_kernels_and_chinese_clip_cpu_gpu_agree(registry, monkeypatch):
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('TRANSFORMERS_OFFLINE', '1')
    probe = gpu_probe()
    assert probe['matrix_max_abs_error'] <= .001
    texts = ('红色正方形', '蓝色圆形', '人物照片')
    image = Image.new('RGB', (224, 224), 'red')
    gpu_model = CLIPModel(registry, device='cuda')
    gpu = gpu_model.infer(image, texts=texts)
    assert len(gpu['embedding']) == 512 and abs(sqrt(sum(v*v for v in gpu['embedding'])) - 1) < .0001
    assert gpu['cosine_scores'][0] > gpu['cosine_scores'][1]
    assert gpu['score_kind'] == 'cosine_not_probability' and gpu['peak_gpu_allocated_bytes'] > 0
    del gpu_model; gc.collect()
    cpu_model = CLIPModel(registry, device='cpu')
    cpu = cpu_model.infer(image, texts=texts)
    assert cpu['device'] == 'cpu' and cpu['peak_gpu_allocated_bytes'] is None
    assert max(abs(a-b) for a,b in zip(cpu['cosine_scores'], gpu['cosine_scores'])) < .002


def test_cuda_failure_does_not_implicitly_fall_back_to_cpu(registry, monkeypatch):
    import torch
    monkeypatch.setattr(torch.cuda, 'is_available', lambda: False)
    with pytest.raises(ModelFailure, match='cuda_unavailable'):
        CLIPModel(registry, device='cuda')
