from app.services.reference_data_service import load_all_reference_data


def test_load_all_reference_data_returns_expected_keys() -> None:
    payload = load_all_reference_data()

    assert set(payload.keys()) == {"freight_rates", "tariffs", "ports", "supplier_seeds"}
    assert len(payload["freight_rates"]) >= 4
    assert len(payload["tariffs"]) >= 1
    assert len(payload["ports"]) >= 5
    assert len(payload["supplier_seeds"]) >= 1


def test_load_all_reference_data_returns_typed_models() -> None:
    payload = load_all_reference_data()

    assert payload["freight_rates"][0].origin_country in {"CN", "TH", "ID"}
    assert payload["tariffs"][0].hs_code == "3902.10"
    assert payload["ports"][0].port_code
    assert payload["supplier_seeds"][0].supplier_name
