from pathlib import Path

import yaml

VALUES = Path(__file__).parents[4] / "infra/helm/values/local/seaweedfs.yaml"


def test_object_data_survives_pod_replacement() -> None:
    values = yaml.safe_load(VALUES.read_text())
    stores = [values["master"]["data"], values["filer"]["data"], *values["volume"]["dataDirs"]]
    assert [store["type"] for store in stores] == ["persistentVolumeClaim"] * 3


def test_volume_sizes_have_one_home() -> None:
    """The Terraform module merges release/storage-sizes.yaml over this file; a size here would be silently
    overridden for every profile."""
    values = yaml.safe_load(VALUES.read_text())
    stores = [values["master"]["data"], values["filer"]["data"], *values["volume"]["dataDirs"]]
    assert not [store for store in stores if "size" in store or "maxVolumes" in store]
