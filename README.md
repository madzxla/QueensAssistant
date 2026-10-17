# Queens Assistant

## Roblox account linking

The `/account link` and `/account view` commands use Roblox OAuth 2.0 and are
separate from `/announce`. Linking requests only the `openid` and `profile`
identity scopes. `/account view` shows the linked Roblox username and profile.

1. Create an OAuth app in the [Roblox Creator Dashboard](https://create.roblox.com/dashboard/credentials?activeTab=OAuthTab).
   Enable the `openid` and `profile` scopes and register the exact redirect URI
   you will use below.
2. Copy `.env.example` to `.env`. Set the OAuth client ID, client secret, and
   redirect URI. The callback path in that URI is handled by this bot.
3. Generate a Fernet key for encrypting saved refresh tokens:

   ```sh
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Set the output as `ROBLOX_OAUTH_TOKEN_KEY`. Keep this key and the client
   secret private. Losing or changing the token key makes saved refresh tokens
   unreadable; users will need to link again.
4. Set `ROBLOX_OAUTH_HOST` and `ROBLOX_OAUTH_PORT` for the callback listener.
   The default listener is `0.0.0.0:8080`. For a public HTTPS redirect URI,
   route its callback path to this listener through a TLS-terminating reverse
   proxy. For local development, Roblox permits an `http://localhost` redirect.
5. Install dependencies with `python -m pip install -r requirements.txt` and
   start the bot. Linked-account refresh tokens are
   encrypted in the SQLite database named by `ROBLOX_OAUTH_DATABASE` (default
   `roblox_accounts.sqlite3`); back up that file and the encryption key securely
   if links should survive host replacement.

OAuth uses PKCE and a short-lived, single-use `state` value. The callback URI
must exactly match the one registered with Roblox. This OAuth service does not
use or change the API key used by `/announce`.
