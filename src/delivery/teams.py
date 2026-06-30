
    def _get_msal_app(self) -> Any:
        if self._msal_app is None:
            import msal
            cache = self._load_cache()
            self._msal_app = msal.PublicClientApplication(
                client_id=self._CLIENT_ID,
                authority="https://login.microsoftonline.com/common",
                token_cache=cache,
            )
        return self._msal_app

    def _acquire_token(self) -> str | None:
        """Return a valid access token, refreshing silently or via device flow."""
        import msal
        app = self._get_msal_app()

        # Try silent refresh first (uses cached refresh token)
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(self._SCOPES, account=accounts[0])
            if result and "access_token" in result:
                self._save_cache(app.token_cache)
                return result["access_token"]

        # No cached token — start interactive device-code flow
        flow = app.initiate_device_flow(scopes=self._SCOPES)
        if "user_code" not in flow:
            logger.error("Failed to initiate device flow: %s", flow.get("error_description"))
            return None

        # Print instructions prominently so the user sees them
        print("\n" + "=" * 60)
        print("🔑  TEAMS AUTHENTICATION REQUIRED")
        print("=" * 60)
        print(flow["message"])
        print("=" * 60 + "\n")

        result = app.acquire_token_by_device_flow(flow)  # blocks until user completes
        if "access_token" in result:
            self._save_cache(app.token_cache)
            logger.info("Teams authentication successful — token cached for future runs")
            return result["access_token"]

        logger.error("Authentication failed: %s", result.get("error_description", result))
        return None

    # ------------------------------------------------------------------
    # Send
    # ------------------------------------------------------------------

    def send(self, payload: dict[str, Any], recipient_email: str) -> bool:
        """POST message + recipient to the flow. The flow routes the DM."""
        body = {
            "recipient": recipient_email,
            "message":   payload.get("message", ""),
        }
        try:
            resp = requests.post(self._url, json=body, timeout=self._timeout)
            if resp.status_code in (200, 202):
                logger.info("Flow DM accepted → %s (HTTP %d)", recipient_email, resp.status_code)
                return True
            logger.error(
                "Flow DM rejected for %s — HTTP %d: %s",
                recipient_email, resp.status_code, resp.text[:200],
            )
            return False
        except requests.RequestException as exc:
            logger.error("Flow DM failed for %s: %s", recipient_email, exc)
            return False
