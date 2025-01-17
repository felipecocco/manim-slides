import hashlib
import os
import shutil
import subprocess
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from pydantic import BaseModel, FilePath, PositiveInt, field_validator, model_validator
from pydantic_extra_types.color import Color
from PySide6.QtCore import Qt

from .defaults import FFMPEG_BIN
from .logger import logger


def merge_basenames(files: List[FilePath]) -> Path:
    """
    Merge multiple filenames by concatenating basenames.
    """
    logger.info(f"Generating a new filename for animations: {files}")

    dirname: Path = files[0].parent
    ext = files[0].suffix

    basenames = (file.stem for file in files)

    basenames_str = ",".join(f"{len(b)}:{b}" for b in basenames)

    # We use hashes to prevent too-long filenames, see issue #123:
    # https://github.com/jeertmans/manim-slides/issues/123
    basename = hashlib.sha256(basenames_str.encode()).hexdigest()

    return dirname.joinpath(basename + ext)


class Key(BaseModel):  # type: ignore
    """Represents a list of key codes, with optionally a name."""

    ids: Set[PositiveInt]
    name: Optional[str] = None

    @field_validator("ids")
    @classmethod
    def ids_is_non_empty_set(cls, ids: Set[Any]) -> Set[Any]:
        if len(ids) <= 0:
            raise ValueError("Key's ids must be a non-empty set")
        return ids

    def set_ids(self, *ids: int) -> None:
        self.ids = set(ids)

    def match(self, key_id: int) -> bool:
        m = key_id in self.ids

        if m:
            logger.debug(f"Pressed key: {self.name}")

        return m


class Config(BaseModel):  # type: ignore
    """General Manim Slides config"""

    QUIT: Key = Key(ids=[Qt.Key_Q], name="QUIT")
    CONTINUE: Key = Key(ids=[Qt.Key_Right], name="CONTINUE / NEXT")
    BACK: Key = Key(ids=[Qt.Key_Left], name="BACK")
    REVERSE: Key = Key(ids=[Qt.Key_V], name="REVERSE")
    REWIND: Key = Key(ids=[Qt.Key_R], name="REWIND")
    PLAY_PAUSE: Key = Key(ids=[Qt.Key_Space], name="PLAY / PAUSE")
    HIDE_MOUSE: Key = Key(ids=[Qt.Key_H], name="HIDE / SHOW MOUSE")

    @model_validator(mode="before")
    def ids_are_unique_across_keys(cls, values: Dict[str, Key]) -> Dict[str, Key]:
        ids: Set[int] = set()

        for key in values.values():
            if len(ids.intersection(key.ids)) != 0:
                raise ValueError(
                    "Two or more keys share a common key code: please make sure each key has distinct key codes"
                )
            ids.update(key.ids)

        return values

    def merge_with(self, other: "Config") -> "Config":
        for key_name, key in self:
            other_key = getattr(other, key_name)
            key.ids.update(other_key.ids)
            key.name = other_key.name or key.name

        return self


class SlideType(str, Enum):
    slide = "slide"
    loop = "loop"
    last = "last"


class SlideConfig(BaseModel):  # type: ignore
    type: SlideType
    start_animation: int
    end_animation: int
    number: int
    terminated: bool = False

    @field_validator("start_animation", "end_animation")
    @classmethod
    def index_is_posint(cls, v: int) -> int:
        if v < 0:
            raise ValueError("Animation index (start or end) cannot be negative")
        return v

    @field_validator("number")
    @classmethod
    def number_is_strictly_posint(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Slide number cannot be negative or zero")
        return v

    @model_validator(mode="before")
    def start_animation_is_before_end(
        cls, values: Dict[str, Union[SlideType, int, bool]]
    ) -> Dict[str, Union[SlideType, int, bool]]:
        if values["start_animation"] >= values["end_animation"]:  # type: ignore
            if values["start_animation"] == values["end_animation"] == 0:
                raise ValueError(
                    "You have to play at least one animation (e.g., `self.wait()`) before pausing. If you want to start paused, use the approriate command-line option when presenting. IMPORTANT: when using ManimGL, `self.wait()` is not considered to be an animation, so prefer to directly use `self.play(...)`."
                )

            raise ValueError(
                "Start animation index must be strictly lower than end animation index"
            )

        return values

    def is_slide(self) -> bool:
        return self.type == SlideType.slide

    def is_loop(self) -> bool:
        return self.type == SlideType.loop

    def is_last(self) -> bool:
        return self.type == SlideType.last

    @property
    def slides_slice(self) -> slice:
        return slice(self.start_animation, self.end_animation)


class PresentationConfig(BaseModel):  # type: ignore
    slides: List[SlideConfig]
    files: List[FilePath]
    resolution: Tuple[PositiveInt, PositiveInt] = (1920, 1080)
    background_color: Color = "black"

    @model_validator(mode="after")
    def animation_indices_match_files(
        cls, config: "PresentationConfig"
    ) -> "PresentationConfig":
        files = config.files
        slides = config.slides

        n_files = len(files)

        for slide in slides:
            if slide.end_animation > n_files:
                raise ValueError(
                    f"The following slide's contains animations not listed in files {files}: {slide}"
                )

        return config

    def copy_to(self, dest: Path, use_cached: bool = True) -> "PresentationConfig":
        """
        Copy the files to a given directory.
        """
        n = len(self.files)
        for i in range(n):
            file = self.files[i]
            dest_path = dest / self.files[i].name
            self.files[i] = dest_path
            if use_cached and dest_path.exists():
                logger.debug(f"Skipping copy of {file}, using cached copy")
                continue
            logger.debug(f"Copying {file} to {dest_path}")
            shutil.copy(file, dest_path)

        return self

    def concat_animations(
        self, dest: Optional[Path] = None, use_cached: bool = True
    ) -> "PresentationConfig":
        """
        Concatenate animations such that each slide contains one animation.
        """

        dest_paths = []

        for i, slide_config in enumerate(self.slides):
            files = self.files[slide_config.slides_slice]

            slide_config.start_animation = i
            slide_config.end_animation = i + 1

            if len(files) > 1:
                dest_path = merge_basenames(files)
                dest_paths.append(dest_path)

                if use_cached and dest_path.exists():
                    logger.debug(f"Concatenated animations already exist for slide {i}")
                    continue

                f = tempfile.NamedTemporaryFile(mode="w", delete=False)
                f.writelines(f"file '{os.path.abspath(path)}'\n" for path in files)
                f.close()

                command: List[str] = [
                    FFMPEG_BIN,
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    f.name,
                    "-c",
                    "copy",
                    str(dest_path),
                    "-y",
                ]
                logger.debug(" ".join(command))
                process = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
                )
                output, error = process.communicate()

                if output:
                    logger.debug(output.decode())

                if error:
                    logger.debug(error.decode())

                if not dest_path.exists():
                    raise ValueError(
                        "could not properly concatenate animations, use `-v INFO` for more details"
                    )

            else:
                dest_paths.append(files[0])

        self.files = dest_paths

        if dest:
            return self.copy_to(dest)

        return self


DEFAULT_CONFIG = Config()
