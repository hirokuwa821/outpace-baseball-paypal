# Outpace Baseball: PayPal AI Hackathon Edition

## Project Overview

Outpace Baseball analyzes a baseball pitcher's motion from smartphone video and presents estimated pitching metrics and coaching feedback. This project packages the PayPal subscription edition as a standalone application. It aims to make useful baseball analysis accessible without expensive motion-capture equipment.

## Problem Statement

Motion-capture systems and specialist analysis services can be expensive or difficult for amateur players to access. Smartphone video is widely available, but turning it into understandable pitching feedback requires computer vision and analysis software.

## Solution

The Streamlit application processes pitching video with the existing computer-vision pipeline, presents its available measurements, and can request AI coaching feedback through the configured AWS integration. A separate local FastAPI service coordinates PayPal subscription creation and verifies the buyer's return with PayPal before persisting an active subscription.

## Key Features

- Video-based pose and pitching analysis using OpenCV and YOLO pose detection.
- Estimated pitch metrics and rule-based analysis, with optional ball tracking when authorized model weights are supplied.
- Streamlit interface with Japanese and English text resources.
- Optional Amazon Bedrock coaching and Amazon S3 archiving when AWS is configured.
- PayPal subscription flow with server-side API calls, one-time pending tokens, and SQLite subscription persistence.
- Automated tests for PayPal pricing, API behavior, state handling, and purchase-flow safeguards.

Analysis results are estimates. Accuracy depends on camera angle, video quality, and model detection.

## PayPal Subscription Integration

The monthly plan is **JPY 1,980/month**. The PayPal SDK obtains an OAuth access token, creates or reuses the configured subscription plan, and creates a subscription approval URL. After the buyer returns, the application checks the pending token and queries PayPal for the subscription, verifies its ID and associated user, and persists access only when the verified status is `ACTIVE`.

The SDK includes a helper that asks PayPal to verify webhook signatures. The FastAPI application does not currently expose a webhook-receiver endpoint or process incoming webhook events; subscription verification in this application uses the buyer return flow and PayPal API.

## Architecture

```text
Smartphone video
	-> Streamlit application
	-> OpenCV / YOLO pose analysis
	-> estimated metrics and coaching interface
		 -> Amazon Bedrock (optional)
		 -> Amazon S3 (optional)

Streamlit purchase UI
	-> local FastAPI service
	-> PayPal Subscriptions API
	-> buyer approval and return
	-> PayPal subscription verification
	-> local SQLite subscription state
```

The Streamlit process starts the local FastAPI child process. PayPal runtime state is stored outside the project directory by default, under the user's local application data directory.

## Technology Stack

- Python
- Streamlit
- FastAPI and Uvicorn
- PayPal Subscriptions API and REST API
- SQLite
- OpenCV, NumPy, pandas, Plotly, and Ultralytics YOLO
- Optional Amazon Bedrock Runtime and Amazon S3 through boto3

## Installation

Use Python 3.12 as the recommended development environment. From the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit the local `.env` with your own PayPal sandbox credentials. Never commit `.env` or paste secrets into issue reports. Set `PAYPAL_ENV=sandbox` while testing.

YOLO model weights and sample videos are not included. Obtain any required weights from an authorized source and follow their applicable license terms. Ball tracking is unavailable unless its required ball-detector weights are supplied locally.

## Environment Variables

| Variable | Description |
| --- | --- |
| `PAYPAL_CLIENT_ID` | PayPal REST application client ID. |
| `PAYPAL_CLIENT_SECRET` | PayPal REST application secret. Keep private. |
| `PAYPAL_ENV` | PayPal environment: `sandbox` or `live`; defaults to `sandbox`. |
| `PAYPAL_WEBHOOK_ID` | PayPal webhook ID used by the SDK signature-verification helper. No webhook receiver is currently exposed. |
| `PAYPAL_JUDGE_TOKEN` | Optional sandbox judge-mode token; keep private. |
| `PAYPAL_JUDGE_MODE` | Enables the judge-only UI behavior when set to `1`; intended for sandbox only. |
| `USE_REVENUECAT` | Billing-provider compatibility flag; set to `0` for this PayPal edition. The original RevenueCat project remains separate. |
| `OUTPACE_PAYPAL_STATE_DIR` | Optional directory for the subscription database, PayPal resource cache, and diagnostics. Defaults to local application data outside the repository. |
| `OUTPACE_MAIN_APP_URL` | Optional destination URL for the post-purchase application link. |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | AWS Region for optional AWS features. |
| `S3_BUCKET` / `AWS_S3_BUCKET_NAME` | Optional S3 bucket for video and analysis archiving. |
| `BEDROCK_MODEL_ID` | Optional Converse-compatible Bedrock model override. |

AWS credentials should come from a standard AWS credential provider such as an AWS profile or an attached role, not from committed files.

## How to Run

With the virtual environment active and `.env` configured:

```powershell
python -m streamlit run src/app.py
```

Open the local URL printed by Streamlit. The application starts its FastAPI service on `127.0.0.1:8000` as needed; do not expose that development service directly to the public internet.

## Testing

Install the test dependencies and run the suite from the project root:

```powershell
python -m pip install -r requirements-test.txt
python -m pytest -q
```

The environment-loading test expects a local `.env` file. Copy `.env.example` as shown above; the example leaves credential fields blank, so live PayPal credentials are not required for the mocked/offline tests. Tests that require a live PayPal or AWS account should be run only in an explicitly configured sandbox or test environment.

## Security Considerations

- `.env`, local databases, logs, caches, generated analysis, videos, and model weights are excluded by `.gitignore`.
- `.env.example` contains variable names and non-secret defaults only; provide credentials locally through environment configuration.
- PayPal credentials and judge tokens must never be committed. Rotate any credential that is accidentally exposed.
- The application verifies subscription identity and status through PayPal before granting persisted access. Do not treat a browser redirect alone as proof of payment.
- Keep PayPal state and any uploaded media in access-controlled local or cloud storage. Use HTTPS and a properly secured deployment before serving the application beyond local development.
- The webhook signature helper is not an event receiver; configure and deploy webhook handling separately if event-driven lifecycle updates are required.
