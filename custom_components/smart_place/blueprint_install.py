"""Copy the shipped automation blueprints into Home Assistant's blueprint folder.

No ``homeassistant`` imports and no imports from this package: the tests
load the module by its path.
"""

from __future__ import annotations

from pathlib import Path
import shutil

BLUEPRINTS = Path(__file__).parent / "blueprints" / "automation"


def install_blueprints(target: Path) -> list[str]:
    """Copy every shipped blueprint that ``target`` does not have yet; return the file names copied.

    An existing file is never overwritten: the user may have changed it
    or re-imported a newer version. Blocking I/O: call it in the executor.
    """
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for source in sorted(BLUEPRINTS.glob("*.yaml")):
        destination = target / source.name
        if not destination.exists():
            shutil.copyfile(source, destination)
            copied.append(source.name)
    return copied
