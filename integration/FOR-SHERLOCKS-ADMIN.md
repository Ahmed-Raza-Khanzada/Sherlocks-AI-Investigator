# Sherlocks side of the integration (for you, not the Laravel developer)

1. **Give the developer:** the `integration/` folder, the address
   `http://192.168.200.239:7401/sindhpolice-sherlocks`, and **privately** the value of
   `SHERLOCKS_AUTH_SECRET` from Sherlocks' `.env` (in person or a password manager, not
   email, chat or the repository).
2. **Keep Sherlocks running** on 192.168.200.239 with docker compose, as now. One API
   process, the default, is required for the live stream.
3. **When the portal's address is known**, set it in Sherlocks' `.env` and restart:
   `SHERLOCKS_CORS_ORIGINS=http://portal-address` (comma-separate several). Until then
   any site may call the API with a valid token. Tokens are still required.
4. **If the portal runs on https**, Sherlocks must too, or browsers block it (mixed
   content): put nginx with a certificate in front of port 7401, and give the developer the
   new https address for `SHERLOCKS_API_BASE`.
5. Your own login to the standalone Sherlocks portal keeps working alongside the tab.
