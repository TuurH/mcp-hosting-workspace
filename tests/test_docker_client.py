"""Docker image imports use the Engine's result, including streamed failures."""
import hashlib
import io
import json
import tarfile

import httpx
import pytest

from mcp_hosting_platform.common.docker_client import DockerClient, DockerError


def image_archive(tags=None):
    config = b'{"architecture":"amd64","os":"linux","rootfs":{"type":"layers","diff_ids":[]}}'
    digest = hashlib.sha256(config).hexdigest()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name, data in {
            digest + ".json": config,
            "manifest.json": json.dumps([{"Config": digest + ".json", "RepoTags": tags, "Layers": []}]).encode(),
        }.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
    archive.seek(0)
    return archive, "sha256:" + digest


def client_with_response(events, expected_image=None):
    requests = []

    def handle(request):
        requests.append(request)
        if request.method == "POST":
            assert request.url.path == "/v1.44/images/load"
            assert request.url.params["quiet"] == "true"
            return httpx.Response(200, text="\n".join(json.dumps(event) for event in events) + "\n")
        assert request.url.path == "/v1.44/images/" + expected_image + "/json"
        return httpx.Response(200, json={"Id": expected_image})

    client = DockerClient()
    client._http.close()
    client._http = httpx.Client(transport=httpx.MockTransport(handle), base_url="http://docker/v1.44")
    return client, requests


@pytest.mark.parametrize("changed_id", [False, True])
def test_load_uses_engine_reported_image_id(changed_id):
    archive, config_id = image_archive()
    loaded_id = "sha256:" + "a" * 64 if changed_id else config_id
    client, requests = client_with_response([
        {"status": "Loading layer"},
        {"stream": "Loaded image ID: " + loaded_id + "\n"},
    ], loaded_id)
    try:
        assert client.load_image(archive, 1024 * 1024) == loaded_id
        assert requests[0].content == archive.getvalue()
        assert len(requests) == 2
    finally:
        client.close()


@pytest.mark.parametrize("event", [
    {"error": "invalid layer archive"},
    {"errorDetail": {"message": "invalid layer archive"}},
])
def test_load_surfaces_error_inside_http_200(event):
    archive, _ = image_archive()
    client, requests = client_with_response([event])
    try:
        with pytest.raises(DockerError, match="invalid layer archive"):
            client.load_image(archive, 1024 * 1024)
        assert len(requests) == 1
    finally:
        client.close()


@pytest.mark.parametrize("events", [
    [],
    [{"stream": "Loaded image ID: invalid\n"}],
    [{"stream": "Loaded image ID: sha256:" + "a" * 64 + "\nLoaded image ID: sha256:" + "b" * 64 + "\n"}],
])
def test_load_requires_one_confirmed_image(events):
    archive, _ = image_archive()
    client, requests = client_with_response(events)
    try:
        with pytest.raises(DockerError, match="exactly one loaded image ID"):
            client.load_image(archive, 1024 * 1024)
        assert len(requests) == 1
    finally:
        client.close()


def test_load_still_refuses_builder_supplied_tags():
    archive, _ = image_archive(["mcp-hosting-workspace/platform:local"])
    client, requests = client_with_response([])
    try:
        with pytest.raises(ValueError, match="one untagged Docker image"):
            client.load_image(archive, 1024 * 1024)
        assert not requests
    finally:
        client.close()
