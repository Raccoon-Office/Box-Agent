# weather-tools
Internal helpers that call the weather service.

## Weather API
- `GET https://api.example.com/v1/weather?city=<city name>`
- Auth: send the key in the `X-API-Key` header.
- Response: JSON `{"city": "...", "temp_c": 21.5, "condition": "cloudy"}`
- The key is `WEATHER_API_KEY` in `.env`.
