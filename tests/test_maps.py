from medical_hours_navi.maps import google_maps_html, map_locations, maps_settings


def facility(facility_id, latitude, longitude, name="Clinic <script>", address="Address </script>"):
    return {
        "facility_id": facility_id,
        "name": name,
        "address": address,
        "latitude": latitude,
        "longitude": longitude,
    }


def test_valid_coordinates_are_included_in_map_data():
    points = map_locations([facility("1", "35.5", "139.7")])
    assert len(points) == 1
    assert points[0]["facility_id"] == "1"
    assert points[0]["latitude"] == 35.5
    assert points[0]["longitude"] == 139.7


def test_zero_zero_is_excluded_from_map_data():
    assert map_locations([facility("1", "0", "0")]) == []


def test_missing_invalid_out_of_range_and_non_finite_coordinates_are_excluded():
    candidates = [
        facility("blank", "", "139.7"),
        facility("invalid", "north", "139.7"),
        facility("range", "91", "139.7"),
        facility("non-finite", "nan", "139.7"),
        facility("infinite", "35.5", "inf"),
    ]
    assert map_locations(candidates) == []


def test_same_coordinate_does_not_deduplicate_facilities():
    points = map_locations([
        facility("1", "35.5", "139.7", name="Clinic A"),
        facility("2", "35.5", "139.7", name="Clinic B"),
    ])
    assert [point["facility_id"] for point in points] == ["1", "2"]


def test_empty_search_results_are_safe():
    assert map_locations([]) == []


def test_maps_configuration_is_optional_for_search():
    assert maps_settings({}) == ("", "")


def test_google_map_html_safely_serializes_external_text():
    html = google_maps_html(map_locations([facility("1", "35.5", "139.7")]), "key", "map-id")
    assert "\\u003cscript\\u003e" in html
    assert "\\u003c/script\\u003e" in html
    assert "textContent" in html
    assert "AdvancedMarkerElement" in html
    assert "maps.googleapis.com/maps/api/js" in html
    assert "GOOGLE_MAPS_API_KEY" not in html
