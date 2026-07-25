from .base import OAuthAdapter


class YouTubeAdapter(OAuthAdapter):
    code = "youtube"
    authorization_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint = "https://oauth2.googleapis.com/token"
    scopes = ("https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly")

    def authorization_params(self, client_id, redirect_uri, state, challenge):
        params = super().authorization_params(client_id, redirect_uri, state, challenge)
        params.update({"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"})
        return params
