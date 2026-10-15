"""2차 검증: head 파일 읽기, 특징 표준화 → softmax → threshold 비교가 reference 와 같은지.

실제 백본(수백 MB)은 쓰지 않고 가짜 전처리기·백본을 끼워 계산만 확인한다.
"""

import pytest

torch = pytest.importorskip("torch")   # 검출 의존성(.[detect])이 없으면 건너뛴다
from types import SimpleNamespace

from PIL import Image

from app.config import PROJECT_ROOT
from app.detector.verifiers import (
    BackboneSpec, BinaryLinearHead, HeadScore, Verifier, dinov2_spec, pool_output, swinv2_spec,
)

DIMENSION = 4


def write_heads(folder, input_scale="mixed", model_id="fake/backbone", threshold=0.5):
    """클래스 1·3·4·5 head 파일을 만든다. 클래스 N 의 head 는 첫 특징값 × N 을 양성 쪽 logit 으로 쓴다."""
    folder.mkdir(parents=True, exist_ok=True)
    for class_id in (1, 3, 4, 5):
        head = BinaryLinearHead(DIMENSION)
        with torch.no_grad():
            head.classifier.weight.zero_()
            head.classifier.bias.zero_()
            head.classifier.weight[1, 0] = class_id
        torch.save({
            "modelId": model_id, "inputScale": input_scale, "featureDimension": DIMENSION,
            "stateDict": head.state_dict(), "threshold": threshold,
            "featureMean": torch.full((DIMENSION,), 1.0), "featureStd": torch.full((DIMENSION,), 2.0),
        }, folder / f"class{class_id}_head.pt")


def spec_for(folder, allowed=("mixed",)) -> BackboneSpec:
    return BackboneSpec("Fake", folder, allowed, "cls")


def fake_backbone(verifier: Verifier, features: torch.Tensor) -> None:
    """이미지 장수만큼 정해 둔 특징을 돌려주는 가짜 전처리기·백본을 끼운다."""
    verifier.processor = lambda images, return_tensors: {"pixel_values": torch.zeros(len(images), 3, 2, 2)}
    verifier.backbone = lambda pixel_values: SimpleNamespace(pooler_output=features[:len(pixel_values)])


def test_표준화한_특징으로_양성_확률을_계산한다(tmp_path):
    write_heads(tmp_path)
    verifier = Verifier(spec_for(tmp_path), torch.device("cpu"))
    fake_backbone(verifier, torch.tensor([[3.0, 0, 0, 0], [1.0, 0, 0, 0]]))
    first, second = verifier.verify([Image.new("RGB", (4, 4))] * 2, [3, 1])
    # (3 - 1) / 2 = 1 → class3 head 의 logit = [0, 3] → softmax[1] = sigmoid(3)
    assert first.score == pytest.approx(float(torch.sigmoid(torch.tensor(3.0))))
    assert first.passed
    # (1 - 1) / 2 = 0 → logit [0, 0] → 0.5. threshold 0.5 와 같으면 정탐 (≥)
    assert second.score == pytest.approx(0.5) and second.passed


def test_threshold_미만이면_오탐():
    assert not HeadScore(score=0.069, threshold=0.07).passed
    assert HeadScore(score=0.07, threshold=0.07).passed


def test_백본은_처음_판정할_때_올린다(tmp_path):
    write_heads(tmp_path)
    verifier = Verifier(spec_for(tmp_path), torch.device("cpu"))
    assert verifier.model_id == "fake/backbone" and not verifier.is_loaded
    assert verifier.verify([], []) == []          # 빈 요청은 백본을 올리지 않는다
    assert not verifier.is_loaded


def test_head_입력_배율이_다르면_거부(tmp_path):
    write_heads(tmp_path, input_scale="2x")
    with pytest.raises(ValueError, match="입력 배율"):
        Verifier(spec_for(tmp_path, allowed=("1x", "mixed")), torch.device("cpu"))


def test_head_파일이_없으면_안내(tmp_path):
    with pytest.raises(FileNotFoundError, match="class1"):
        Verifier(spec_for(tmp_path / "없음"), torch.device("cpu"))


def test_특징_꺼내기는_pooler_output_우선():
    hidden = torch.tensor([[[1.0, 2.0], [3.0, 4.0]]])                 # [1장, 토큰 2개, 차원 2]
    assert pool_output(SimpleNamespace(pooler_output=torch.ones(1, 2), last_hidden_state=hidden), "cls").tolist() == [[1, 1]]
    no_pooler = SimpleNamespace(pooler_output=None, last_hidden_state=hidden)
    assert pool_output(no_pooler, "cls").tolist() == [[1, 2]]         # DINOv2: 첫 토큰(CLS)
    assert pool_output(no_pooler, "mean").tolist() == [[2, 3]]        # SwinV2: 토큰 평균


MODELS = PROJECT_ROOT / "models"


@pytest.mark.skipif(not (MODELS / "swinv2").is_dir(), reason="models 에 head 파일이 없다")
def test_실제_head_파일의_threshold():
    swin = Verifier(swinv2_spec(MODELS), torch.device("cpu"))
    dino = Verifier(dinov2_spec(MODELS), torch.device("cpu"))
    assert {c: round(float(cp["threshold"]), 2) for c, (_, cp) in swin.heads.items()} == {1: 0.16, 3: 0.21, 4: 0.56, 5: 0.05}
    assert {c: round(float(cp["threshold"]), 2) for c, (_, cp) in dino.heads.items()} == {1: 0.07, 3: 0.12, 4: 0.7, 5: 0.05}
