from pathlib import Path

import yaml

VALUES = Path(__file__).parents[3] / "infra/helm/values/local/seaweedfs.yaml"


def test_object_data_survives_pod_replacement() -> None:
    values = yaml.safe_load(VALUES.read_text())
    stores = [values["master"]["data"], values["filer"]["data"], *values["volume"]["dataDirs"]]
    assert [store["type"] for store in stores] == ["persistentVolumeClaim"] * 3
    assert all(store["size"] for store in stores)
