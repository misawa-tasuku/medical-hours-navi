"""Google Maps JavaScript API helpers for displaying search results."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence


def maps_settings(secrets: Mapping[str, object]) -> tuple[str, str]:
    """Read optional Maps credentials without making them a search dependency."""
    return str(secrets.get("GOOGLE_MAPS_API_KEY", "") or ""), str(
        secrets.get("GOOGLE_MAP_ID", "") or ""
    )


def map_locations(results: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    """Return map-ready facilities, retaining only finite, in-range coordinates."""
    locations: list[dict[str, object]] = []
    for result in results:
        try:
            latitude = float(result.get("latitude", ""))
            longitude = float(result.get("longitude", ""))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(latitude) or not math.isfinite(longitude):
            continue
        if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            continue
        if latitude == 0 and longitude == 0:
            continue
        locations.append({
            "facility_id": str(result.get("facility_id", "")),
            "name": str(result.get("name", "")),
            "address": str(result.get("address", "")),
            "latitude": latitude,
            "longitude": longitude,
        })
    return locations


def google_maps_html(locations: Sequence[Mapping[str, object]],
                     api_key: str, map_id: str) -> str:
    """Build a self-contained HTML map with escaped JSON configuration."""
    config = json.dumps(
        {"locations": list(locations), "apiKey": api_key, "mapId": map_id},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    # Prevent JSON data from terminating its script element or forming HTML.
    config = (config.replace("&", "\\u0026").replace("<", "\\u003c")
              .replace(">", "\\u003e").replace("'", "\\u0027"))
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  #medical-results-map {{ width: 100%; height: 460px; }}
  #medical-results-map-status {{ padding: 1rem; font: 14px sans-serif; color: #8b1e1e; }}
</style></head><body>
<div id="medical-results-map" role="application" aria-label="検索結果の医療機関マップ"></div>
<div id="medical-results-map-status" role="status" hidden></div>
<script id="map-config" type="application/json">{config}</script>
<script>
(() => {{
  const config = JSON.parse(document.getElementById('map-config').textContent);
  const status = document.getElementById('medical-results-map-status');
  const showError = () => {{
    document.getElementById('medical-results-map').hidden = true;
    status.hidden = false;
    status.textContent = 'Google Mapsを読み込めませんでした。検索結果一覧は引き続き利用できます。';
  }};
  window.gm_authFailure = showError;
  window.initMedicalMap = async () => {{
    try {{
      const [{{ Map }}, {{ AdvancedMarkerElement }}] = await Promise.all([
        google.maps.importLibrary('maps'), google.maps.importLibrary('marker')
      ]);
      const points = config.locations;
      const first = points[0];
      const uniquePositions = new Set(points.map(point => point.latitude + ',' + point.longitude));
      const map = new Map(document.getElementById('medical-results-map'), {{
        center: {{ lat: first.latitude, lng: first.longitude }},
        zoom: uniquePositions.size > 1 ? 12 : 15,
        mapId: config.mapId,
      }});
      const bounds = new google.maps.LatLngBounds();
      const infoWindow = new google.maps.InfoWindow();
      for (const point of points) {{
        const position = {{ lat: point.latitude, lng: point.longitude }};
        const marker = new AdvancedMarkerElement({{
          map, position, title: point.name || '名称未登録', gmpClickable: true
        }});
        bounds.extend(position);
        marker.addEventListener('gmp-click', () => {{
          const content = document.createElement('div');
          const name = document.createElement('strong');
          name.textContent = point.name || '名称未登録';
          const address = document.createElement('p');
          address.textContent = point.address || '住所未登録';
          const link = document.createElement('a');
          link.textContent = 'Google Mapsで開く';
          link.href = 'https://www.google.com/maps/search/?api=1&query='
            + encodeURIComponent(point.latitude + ',' + point.longitude);
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
          content.append(name, address, link);
          infoWindow.setContent(content);
          infoWindow.open({{ map, anchor: marker }});
        }});
      }}
      if (uniquePositions.size > 1) {{
        map.fitBounds(bounds, 48);
        google.maps.event.addListenerOnce(map, 'bounds_changed', () => {{
          if (map.getZoom() > 16) map.setZoom(16);
        }});
      }}
    }} catch (error) {{
      showError();
    }}
  }};
  const script = document.createElement('script');
  script.src = 'https://maps.googleapis.com/maps/api/js?key='
    + encodeURIComponent(config.apiKey)
    + '&v=weekly&loading=async&libraries=marker&callback=initMedicalMap';
  script.async = true;
  script.onerror = showError;
  document.head.appendChild(script);
}})();
</script></body></html>"""
