from yoomoney import Authorize


CLIENT_ID = "PASTE_CLIENT_ID_HERE"
REDIRECT_URI = "https://discord.com"
CLIENT_SECRET = "PASTE_CLIENT_SECRET_HERE"


Authorize(
    client_id=CLIENT_ID,
    redirect_uri=REDIRECT_URI,
    client_secret=CLIENT_SECRET,
    scope=[
        "account-info",
        "operation-history",
        "operation-details",
        "incoming-transfers",
        "payment-p2p",
        "payment-shop",
    ],
)
