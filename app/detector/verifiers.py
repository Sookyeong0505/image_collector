"""2차 검증: 고정된 백본 + 클래스별 linear head (명세 R11).

reference/server/pipelines/{swinv2,dinov2}/verifier.py 와 common.py 의 BinaryLinearHead, pool_output 을 옮겼다.
두 verifier 는 백본 이름, 특징을 꺼내는 방법, 허용하는 입력 배율만 달라서 클래스 하나로 합쳤다.

    Crop → 이미지 전처리기 → 백본 → 특징 벡터 → (f - 평균) / 표준편차 → Linear → softmax[1] ≥ threshold
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import torch
from PIL import Image
from torch import nn

log = logging.getLogger(__name__)

SWINV2_RUN = "run_20260915_161151"
DINOV2_RUN = "run_20260915_163347"
CLASS_IDS = (1, 3, 4, 5)


class BinaryLinearHead(nn.Module):
    def __init__(self, feature_dimension: int) -> None:
        super().__init__()
        self.classifier = nn.Linear(feature_dimension, 2)

    def forward(self, features: torch.Tensor) -> torch.Tensor:
        return self.classifier(features)


@dataclass(frozen=True)
class HeadScore:
    score: float       # softmax 의 양성 확률 (reference 의 adultScore)
    threshold: float   # head 파일에 저장된 클래스별 기준

    @property
    def passed(self) -> bool:
        return self.score >= self.threshold


@dataclass(frozen=True)
class BackboneSpec:
    name: str                          # 로그·에러 메시지용
    model_dir: Path                    # class{N}_head.pt 가 있는 폴더
    allowed_scales: tuple[str, ...]    # head 파일의 inputScale 이 이 중 하나여야 한다
    fallback_pooling: str              # pooler_output 이 없을 때: 'cls' (첫 토큰) | 'mean' (토큰 평균)


def swinv2_spec(model_root: Path) -> BackboneSpec:
    return BackboneSpec("SwinV2", model_root / "swinv2" / SWINV2_RUN, ("1x", "mixed"), "mean")


def dinov2_spec(model_root: Path) -> BackboneSpec:
    return BackboneSpec("DINOv2", model_root / "dinov2" / DINOV2_RUN, ("1.5x", "mixed"), "cls")


def resolve_device(requested: str) -> torch.device:
    if requested != "auto":
        return torch.device(requested)
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def pool_output(outputs, fallback: str) -> torch.Tensor:
    """백본 출력에서 이미지 1장당 특징 벡터 1개를 꺼낸다. pooler_output 이 있으면 그것을 쓴다."""
    pooler_output = getattr(outputs, "pooler_output", None)
    if pooler_output is not None:
        return pooler_output
    last_hidden_state = getattr(outputs, "last_hidden_state", None)
    if last_hidden_state is None:
        raise RuntimeError("백본 출력에서 특징 벡터를 찾지 못했습니다.")
    return last_hidden_state[:, 0] if fallback == "cls" else last_hidden_state.mean(dim=1)


class Verifier:
    """head 는 바로 읽고, 무거운 백본은 처음 verify 할 때 올린다 (reference 와 같은 lazy loading)."""

    def __init__(self, spec: BackboneSpec, device: torch.device):
        self.spec = spec
        self.device = device
        self.model_id: str | None = None
        self.heads: dict[int, tuple[BinaryLinearHead, dict]] = {}
        self.processor = None
        self.backbone = None
        for class_id in CLASS_IDS:
            self._load_head(class_id)

    @property
    def is_loaded(self) -> bool:
        return self.backbone is not None

    def _load_head(self, class_id: int) -> None:
        path = self.spec.model_dir / f"class{class_id}_head.pt"
        if not path.is_file():
            raise FileNotFoundError(f"{self.spec.name} class{class_id} head 가 없습니다: {path}")
        # weights_only: 텐서·숫자·문자열만 읽는다. 파일 안의 임의 코드가 실행되지 않는다.
        checkpoint = torch.load(path, map_location="cpu", weights_only=True)
        if checkpoint.get("inputScale") not in self.spec.allowed_scales:
            raise ValueError(
                f"{self.spec.name} class{class_id} head 의 입력 배율({checkpoint.get('inputScale')})이 "
                f"{self.spec.allowed_scales} 중 하나가 아닙니다."
            )
        model_id = str(checkpoint["modelId"])
        if self.model_id is None:
            self.model_id = model_id
        elif self.model_id != model_id:
            raise ValueError(f"{self.spec.name} 클래스별 head 의 백본이 서로 다릅니다.")
        head = BinaryLinearHead(int(checkpoint["featureDimension"]))
        head.load_state_dict(checkpoint["stateDict"])
        head.eval()
        self.heads[class_id] = (head, checkpoint)

    def load_backbone(self) -> None:
        if self.is_loaded:
            return
        from transformers import AutoImageProcessor, AutoModel
        from transformers.utils import logging as hf_logging

        # 백본의 분류층(classifier)을 쓰지 않는다는 경고 표가 길게 찍힌다. 우리는 특징만 쓰므로 정상이다.
        hf_logging.set_verbosity_error()
        log.info("%s 백본 로딩: %s (%s)", self.spec.name, self.model_id, self.device)
        self.processor = AutoImageProcessor.from_pretrained(self.model_id)
        backbone = AutoModel.from_pretrained(self.model_id).to(self.device).eval()
        for parameter in backbone.parameters():
            parameter.requires_grad = False
        self.backbone = backbone
        log.info("%s 백본 로딩 완료", self.spec.name)

    def verify(self, images: list[Image.Image], class_ids: list[int]) -> list[HeadScore]:
        """images[i] 를 class_ids[i] 의 head 로 판정한다. 여러 장을 한 번에 백본에 넣는다."""
        if len(images) != len(class_ids):
            raise ValueError(f"{self.spec.name} 이미지와 classId 개수가 다릅니다.")
        if not images:
            return []
        self.load_backbone()

        with torch.inference_mode():
            inputs = self.processor(images=images, return_tensors="pt")
            inputs = {name: value.to(self.device) for name, value in inputs.items()}
            features = pool_output(self.backbone(**inputs), self.spec.fallback_pooling).detach().float().cpu()
            scores = []
            for index, class_id in enumerate(class_ids):
                head, checkpoint = self.heads[class_id]
                standardized = (
                    features[index:index + 1] - checkpoint["featureMean"]
                ) / checkpoint["featureStd"].clamp_min(1e-6)
                score = float(torch.softmax(head(standardized), dim=1)[0, 1])
                scores.append(HeadScore(score, float(checkpoint["threshold"])))
            return scores
