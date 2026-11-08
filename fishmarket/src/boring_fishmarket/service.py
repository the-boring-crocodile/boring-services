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

TransferID = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$", description="Transfer id from create_transfer or list_transfers.")]
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
            "Temporary file handovers. MCP carries receipts only; bytes go over HTTP: deposit "
            "with the transfer's token, collect with the collection receipt's token (or the "
            "local streaming helper with a service bearer). Browser URLs/tokens are sensitive capabilities: give them "
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
        "Create a one-file transfer. For a download, PUT the file to upload_url with "
        "X-Fishmarket-Token set to upload_token, exact Content-Length, and X-Fishmarket-Filename "
        "set to the percent-encoded filename. Then call open_download. For an upload, give the "
        "intended person the url and separate token. Use open_url only if returned. It embeds "
        "the token. Keep tokens out of chat, notes and logs.",
    )
    def create_transfer(
        direction: Annotated[Literal["upload", "download"], Field(description=(
            "upload lets a person send you a file. download lets you send them a file."))],
        name: Annotated[str, Field(description=(
            "Plain filename up to 240 UTF-8 bytes. Required for download."))] = "",
        size: Annotated[Bytes | None, Field(description="Exact file size in bytes. Download only.")] = None,
        sha: Annotated[Digest | None, Field(description="Lowercase SHA-256 of the file bytes. Download only.")] = None,
        max_bytes: Annotated[Bytes | None, Field(
            description="Upload only. Reserved bytes, up to the configured file limit (also the default).",
            json_schema_extra={"minimum": 1},
        )] = None,
    ):
        receipt = store.create(calling_author(), direction, name, size, sha, max_bytes)
        receipt["upload_url"] = base + "/api/" + receipt["id"] + "/upload"
        if direction == "upload":
            receipt["token"] = receipt.pop("upload_token")
            receipt = browser_receipt(base, receipt, unsafe_url_auth)
        return receipt

    @tool(
        "writer",
        "Issue a fresh download link after depositing the file. Give the recipient the url and "
        "separate token, or open_url if enabled. It contains the token. The first browser to "
        "press Download gets a ten-minute retry window. Keep tokens out of chat, notes and logs.",
    )
    def open_download(transfer_id: TransferID):
        return browser_receipt(base, store.open_download(transfer_id, calling_author()), unsafe_url_auth)

    @tool(
        "reader", "Read transfer state without tokens. Use open_download for a ready deposit or collection_receipt for a ready upload."
    )
    def transfer_status(transfer_id: TransferID):
        return store.status(transfer_id, calling_author())

    @tool(
        "reader",
        "Read up to 100 transfers you created. Use revoke_transfer for abandoned slots or open_download for a ready deposit.",
    )
    def list_transfers():
        return store.list_transfers(calling_author())

    @tool(
        "reader",
        "Read up to 100 uploads you can collect. Poll after a person uploads, then use collection_receipt for the file.",
    )
    def list_ready():
        return store.list_ready(calling_author())

    @tool(
        "reader",
        "Get a new token for a ready upload. GET download_url with X-Fishmarket-Token, saving to a "
        "new file. Reads and Range resumes work for ten minutes or until transfer expiry. A new "
        "receipt replaces the token. Keep it out of chat, notes and logs. Verify size and sha "
        "against the receipt, then call acknowledge.",
    )
    def collection_receipt(transfer_id: TransferID):
        row = store.collection_receipt(transfer_id, calling_author())
        return {**row, "download_url": base + "/api/" + transfer_id + "/collect"}

    # Reader on purpose: the acknowledgement completes a collection, which a reader
    # may make; storage limits it to the creator or a granted collector. It deletes
    # the payload, hence writes=True.
    @tool(
        "reader",
        "Confirm an upload after saving a verified copy without overwriting a file. Deletes the "
        "server's copy. If the reply is lost, repeat acknowledge with the same id, size and sha.",
        writes=True,
    )
    def acknowledge(
        transfer_id: TransferID,
        size: Annotated[Bytes, Field(description="Verified local file size in bytes.")],
        sha: Annotated[Digest, Field(description="Verified lowercase SHA-256 of the local file.")],
    ):
        return store.ack(transfer_id, calling_author(), size, sha)

    @tool(
        "writer",
        "Let a specific signed-in reader collect your upload, or remove that permission. The "
        "person needs existing service access. They use list_ready and collection_receipt.",
    )
    def grant_collector(
        transfer_id: TransferID,
        collector: Annotated[str, Field(
            description='Exact signed-in author id, usually a JSON string like ["key:example","reader"].',
            json_schema_extra={"minLength": 1, "maxLength": 512},
        )],
        enabled: Annotated[bool, Field(strict=True, description="True grants access. False removes it.")] = True,
    ):
        return store.grant(transfer_id, calling_author(), collector, enabled)

    @tool(
        "writer",
        "Delete your transfer and invalidate its links and tokens. Active streams stop on their next chunk.",
    )
    def revoke_transfer(transfer_id: TransferID):
        return store.revoke(transfer_id, calling_author())

    refuse_unknown_arguments(server)
    return server
