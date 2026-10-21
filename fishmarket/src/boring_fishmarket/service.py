"""MCP control plane. Service-kit owns identity, permission and outcomes."""

from typing import Annotated, Literal
from pydantic import Field
from mcp.server.mcpserver import MCPServer
from boring_service_kit.access import calling_author, permitted
from boring_service_kit.calllog import CallLog
from boring_service_kit.tools import (
    as_mcp_tool,
    refuse_unknown_arguments,
    READ_ONLY,
    WRITES,
)
from boring_service_kit.pond import attach_heartbeat

TransferID = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Bytes = Annotated[int, Field(strict=True, ge=0)]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
UNSAFE_URL_AUTH_PARAM = "this-is-unsafe-dont-do-this-auth"


def browser_receipt(base, receipt, unsafe_url_auth=False):
    """Presentation belongs here; storage knows capabilities, not public URLs.

    The clean `url` and the `token` always; `open_url` (the token in the link's
    fragment) only with the opt-in --unsafe-url-auth, as Canvas."""
    url = base + "/" + receipt["direction"] + "/" + receipt["id"]
    answer = {**receipt, "url": url}
    if unsafe_url_auth:
        answer["open_url"] = url + "#" + UNSAFE_URL_AUTH_PARAM + "=" + receipt["token"]
    return answer


def build_server(store, base, config, unsafe_url_auth=False):
    server = MCPServer(
        "boring-fishmarket",
        instructions=(
            "Temporary file handovers. MCP carries receipts only; use the local streaming helper "
            "to deposit/collect bytes. Browser URLs/tokens are sensitive capabilities: give them "
            "to the intended person only, never logs or shared notes. Upload readiness requires "
            "list_ready or transfer_status; discovery does not notify the model."
        ),
        **config.auth,
    )
    attach_heartbeat(server, config.on_discover)

    def tool(role, description, *, writes=None):
        def register(fn):
            mutates = role == "writer" if writes is None else writes
            server.tool(
                description=description, annotations=WRITES if mutates else READ_ONLY
            )(as_mcp_tool(permitted(fn, role), CallLog(None)))
            return fn

        return register

    @tool(
        "writer",
        "Create a one-file transfer. Download deposits need exact filename/size/SHA256. Upload slots reserve max_bytes (default configured file limit). Expires after 24h by default.",
    )
    def create_transfer(
        direction: Literal["upload", "download"],
        name: str = "",
        size: Bytes | None = None,
        sha: Digest | None = None,
        max_bytes: Bytes | None = None,
    ):
        receipt = store.create(calling_author(), direction, name, size, sha, max_bytes)
        receipt["upload_url"] = base + "/api/" + receipt["id"] + "/upload"
        if direction == "upload":
            receipt["token"] = receipt.pop("upload_token")
            receipt = browser_receipt(base, receipt, unsafe_url_auth)
        return receipt

    @tool(
        "writer",
        "Issue/rotate the browser download capability after complete deposit. GET/HEAD do not claim. The first Download button claim wins a fixed ten-minute retry session.",
    )
    def open_download(transfer_id: TransferID):
        return browser_receipt(base, store.open_download(transfer_id, calling_author()), unsafe_url_auth)

    @tool(
        "reader", "Read one owned or explicitly granted transfer; no secrets returned."
    )
    def transfer_status(transfer_id: TransferID):
        return store.status(transfer_id, calling_author())

    @tool(
        "reader",
        "List up to 100 owned transfers without capability secrets, for recovery after lost receipts. Revoke abandoned pending slots; rotate ready download links with open_download.",
    )
    def list_transfers():
        return store.list_transfers(calling_author())

    @tool(
        "reader",
        "List up to 100 completed inbound uploads this verified caller may collect. No automatic model notification.",
    )
    def list_ready():
        return store.list_ready(calling_author())

    @tool(
        "reader",
        "Return receipt and authenticated streaming URL for an owned/granted ready upload; use helper, not model context.",
    )
    def collection_receipt(transfer_id: TransferID):
        row = store.collection_receipt(transfer_id, calling_author())
        return {**row, "download_url": base + "/api/" + transfer_id + "/collect"}

    # Reader on purpose: the acknowledgement completes a collection, which a reader
    # may make; storage limits it to the creator or a granted collector. It deletes
    # the payload, hence writes=True.
    @tool(
        "reader",
        "Acknowledge an inbound file only after local size/SHA256 verification and no-overwrite publication. Deletes service payload; identical receipt retries are safe.",
        writes=True,
    )
    def acknowledge(transfer_id: TransferID, size: Bytes, sha: Digest):
        return store.ack(transfer_id, calling_author(), size, sha)

    @tool(
        "writer",
        "Owner grants/revokes collection of one inbound transfer to an exact verified service author ID, normally JSON [issuer,subject]; does not enroll the caller into service access.",
    )
    def grant_collector(
        transfer_id: TransferID,
        collector: str,
        enabled: Annotated[bool, Field(strict=True)] = True,
    ):
        return store.grant(transfer_id, calling_author(), collector, enabled)

    @tool(
        "writer",
        "Owner revokes all capabilities and deletes a transfer; active streams stop on their next chunk.",
    )
    def revoke_transfer(transfer_id: TransferID):
        return store.revoke(transfer_id, calling_author())

    refuse_unknown_arguments(server)
    return server
