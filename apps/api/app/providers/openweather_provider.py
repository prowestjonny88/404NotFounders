from __future__ import annotations

import httpx

from app.core.exceptions import ProviderError


class OpenWeatherProvider:
    FORECAST_5_DAY_URL = "https://api.openweathermap.org/data/2.5/forecast"
    ONE_CALL_URL = "https://api.openweathermap.org/data/3.0/onecall"
    CLIMATE_30_DAY_URL = "https://pro.openweathermap.org/data/2.5/forecast/climate"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    async def fetch_forecast(self, *, latitude: float, longitude: float) -> dict:
        """
        Fetch the longest live forecast this key can access.

        OpenWeather's 30-day climate forecast is a Pro endpoint. If this key is
        not subscribed, we try One Call daily forecasts, then the standard
        5-day/3-hour endpoint. This is an endpoint capability fallback only:
        the returned payload is still live OpenWeather data and is labelled
        with the endpoint that actually succeeded.
        """
        attempts: list[str] = []
        endpoints = (
            (
                self.CLIMATE_30_DAY_URL,
                {"lat": latitude, "lon": longitude, "appid": self.api_key, "units": "metric", "cnt": 30},
                "forecast_climate_30_day",
            ),
            (
                self.ONE_CALL_URL,
                {
                    "lat": latitude,
                    "lon": longitude,
                    "appid": self.api_key,
                    "units": "metric",
                    "exclude": "current,minutely,alerts",
                },
                "onecall_8_day",
            ),
            (
                self.FORECAST_5_DAY_URL,
                {"lat": latitude, "lon": longitude, "appid": self.api_key, "units": "metric"},
                "forecast_5_day_3_hour",
            ),
        )
        async with httpx.AsyncClient(timeout=20.0) as client:
            for url, params, endpoint_name in endpoints:
                try:
                    response = await client.get(url, params=params)
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ProviderError("OpenWeather response was not a JSON object.")
                    payload["_openweather_endpoint"] = endpoint_name
                    payload["_openweather_url"] = url
                    payload["_openweather_attempts"] = [*attempts, f"{endpoint_name}:success"]
                    return payload
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    attempts.append(f"{endpoint_name}:{status}")
                    if status in {401, 403, 404, 429}:
                        continue
                    continue
                except Exception as exc:
                    attempts.append(f"{endpoint_name}:{exc}")
                    continue

        raise ProviderError(
            f"OpenWeatherMap request failed for ({latitude},{longitude}); attempted endpoints: {', '.join(attempts)}"
        )
