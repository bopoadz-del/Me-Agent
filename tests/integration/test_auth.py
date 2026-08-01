import hashlib, hmac as hmac_mod
import httpx, pytest
import websockets

pytestmark = pytest.mark.asyncio

async def test_unknown_agent_and_bad_proof_rejected(hive):
    async with httpx.AsyncClient(base_url=hive.url) as c:
        r = await c.get("/auth/challenge", params={"agent_id": "ghost"})
        # Challenge may be issued blindly (no enumeration), but proof must fail:
        nonce = r.json()["nonce"]
        r2 = await c.post("/auth/prove", json={
            "agent_id": "ghost", "nonce": nonce, "proof": "00" * 32})
        assert r2.status_code == 401

async def test_challenge_response_issues_jwt(hive, enrolled_agent):
    agent_id, secret = enrolled_agent
    async with httpx.AsyncClient(base_url=hive.url) as c:
        nonce = (await c.get("/auth/challenge",
                             params={"agent_id": agent_id})).json()["nonce"]
        proof = hmac_mod.new(secret.encode(), nonce.encode(),
                             hashlib.sha256).hexdigest()
        r = await c.post("/auth/prove", json={
            "agent_id": agent_id, "nonce": nonce, "proof": proof})
        assert r.status_code == 200
        assert r.json()["session_token"]
        # Nonce is single-use:
        r2 = await c.post("/auth/prove", json={
            "agent_id": agent_id, "nonce": nonce, "proof": proof})
        assert r2.status_code == 401

async def test_ws_sync_rejects_unauthenticated(hive):
    async with websockets.connect(hive.ws_url) as ws:
        await ws.send('{"auth": "not-a-jwt"}')
        with pytest.raises(websockets.ConnectionClosed) as exc:
            await ws.recv()
        assert exc.value.rcvd.code == 4401

async def test_mission_api_requires_bearer(agent_api):
    async with httpx.AsyncClient(base_url=agent_api.url) as c:
        r = await c.post("/mission", json={"natural_language": "x"})
        assert r.status_code == 401
        r = await c.get("/health")
        assert r.status_code == 200
