# op9

op9 answers an apartment intercom through Twilio. When a visitor enters the correct passcode, the service sends the keypad digit `9` back to the intercom to unlock the door.

## Visitor entry

```mermaid
sequenceDiagram
    participant Panel as Visitor at intercom
    participant Twilio
    participant API as op9 on Cloud Run
    Panel->>Twilio: Call service number
    Twilio->>API: POST /voice
    API-->>Twilio: Prompt and gather keypad digits
    Twilio-->>Panel: Enter the passcode
    Panel->>Twilio: Enter digits
    Twilio->>API: POST /passcode with digits
    API->>API: Compare digits with configured passcode
    API->>Twilio: Submit approved or denied SMS
    Twilio-->>API: Accept message for delivery
    Note over Twilio: SMS delivery continues independently
    alt Correct code
        API-->>Twilio: Say Opening and play unlock digits
        Twilio-->>Panel: Return keypad digit 9
    else Incorrect code
        API-->>Twilio: Say incorrect passcode and hang up
        Twilio-->>Panel: Play denial and end call
    end
```

op9 waits for SMS submission before returning the door instructions. Delivery to the handset can happen later. If submission fails, op9 still returns the passcode decision. If the visitor enters nothing, Twilio hangs up without calling `/passcode` or sending a summary.

## Live phone test

A separate Twilio number plays the visitor. After sending the passcode, it uses Twilio’s `Gather` to listen for one returned keypad digit.

```mermaid
sequenceDiagram
    participant Test as pytest
    participant Caller as Twilio test caller
    participant Service as op9 service number
    participant API as op9 callback
    participant Logs as Cloud Logging
    Test->>Caller: Start call and retain caller CallSid
    Caller->>Service: Call and send passcode
    Service-->>Caller: Return unlock digit over the call
    Caller->>API: Signed POST /phone-test/dtmf with CallSid and Digits
    API->>Logs: Log digit and caller CallSid after validating signature
    API-->>Caller: Hang up
    Test->>Logs: Poll for that CallSid
    Logs-->>Test: Return received digit
    Test->>Test: Pass only for 9
```

The assertion verifies what the test caller received over the phone. The physical lock and SMS delivery are outside its scope. Set `E2E_CALLER_NUMBER` on the service to label that caller’s SMS summaries with `[TEST]`.

## Run and deploy

With Python 3.13 and uv, run the offline suite from the repository root:

```bash
MODE=auto uv run pytest -m 'not phone_e2e' -q
```

- [Deploy the passcode service](docs/deployment.md) covers Cloud Run configuration, Twilio webhooks, and notification logs.
- [Run the live phone test](docs/live-test.md) covers credentials, the opt-in command, and the saved test result.
- [Read the experiment notes](docs/experiments.md) for automatic entry, voice-agent screening, personal challenges, and SMS approvals.
