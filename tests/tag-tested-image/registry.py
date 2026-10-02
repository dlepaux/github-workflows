"""The two things run.sh asks of its registry, over the registry's own HTTP API.

seed <base> <repo>         puts two images there and prints the digest of the second: an
                           older one tagged `latest`, and the tested one with no tag at all,
                           as a build with a test-script leaves it. Nothing is asked of Docker Hub.
holds <base> <repo> <ref>  prints the image the tag or digest leads to: its own digest, or,
                           for an index (imagetools wraps a lone image in one), every image in it.
"""
import hashlib
import json
import sys
import urllib.parse
import urllib.request

MANIFEST = "application/vnd.oci.image.manifest.v1+json"
INDEX = "application/vnd.oci.image.index.v1+json"


def send(method, url, data=None, headers=None):
    return urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=headers or {}))


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def image(base, repo, which, tag=None):
    config = json.dumps({"architecture": "amd64", "os": "linux", "config": {"Labels": {"which": which}},
                         "rootfs": {"type": "layers", "diff_ids": []}}).encode()
    upload = urllib.parse.urljoin(base, send("POST", f"{base}/v2/{repo}/blobs/uploads/").headers["Location"])
    send("PUT", upload + ("&" if "?" in upload else "?") + "digest=" + digest(config), config,
         {"Content-Type": "application/octet-stream"})
    manifest = json.dumps({"schemaVersion": 2, "mediaType": MANIFEST, "layers": [], "config": {
        "mediaType": "application/vnd.oci.image.config.v1+json", "digest": digest(config), "size": len(config)}}).encode()
    send("PUT", f"{base}/v2/{repo}/manifests/{tag or digest(manifest)}", manifest, {"Content-Type": MANIFEST})
    return digest(manifest)


def holds(base, repo, ref):
    answer = send("GET", f"{base}/v2/{repo}/manifests/{ref}", headers={"Accept": f"{INDEX}, {MANIFEST}"})
    body = answer.read()
    if json.loads(body)["mediaType"] == INDEX:
        return " ".join(m["digest"] for m in json.loads(body)["manifests"])
    return digest(body)


if __name__ == "__main__":
    command, base, repo, *rest = sys.argv[1:]
    if command == "seed":
        image(base, repo, "the image latest was", "latest")
        print(image(base, repo, "the image that was tested"))
    else:
        print(holds(base, repo, rest[0]))
