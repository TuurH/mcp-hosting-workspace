"""Which images may be deleted, and deleting them.

Automatic cleanup only ever removes images the platform created and recorded
itself (a Dockerfile build, a development snapshot), addressed by their exact
image ID, and only after checking again that no revision the platform may still
start refers to them. Everything else Docker holds (leftovers from platform
rebuilds, images the owner tagged) is listed for the owner and removed only on
the owner's click.
"""
from __future__ import annotations

import logging
import json

logger = logging.getLogger('mcp-hosting.images')


class Images:
    def __init__(self, runtime) -> None:
        self.runtime, self.platform, self.db, self.docker = runtime, runtime.platform, runtime.db, runtime.docker

    def recorded(self, name: str, version: str) -> set[str]:
        """Image IDs the platform itself created for this revision."""
        ids: set[str] = set()
        built = self.db.get_setting(f'built-image:{name}:{version}')
        if built and built.get('image'):
            ids.add(built['image'])
        manifest = self.runtime.manifest(name, version) or {}
        image = str(manifest.get('image', ''))
        if manifest.get('image_source') and 'dockerfile' not in manifest and image.startswith('sha256:'):
            ids.add(image)  # a development snapshot
        return ids

    def kept_versions(self, app: dict) -> set[str]:
        keep = {v for v in (app['live_version'], app['pending_version'], app['previous_version']) if v}
        development = self.db.get_setting('development:' + app['name'])
        if development and development.get('version'):
            keep.add(development['version'])
        for row in self.db.query("SELECT result FROM operations WHERE app=? AND status IN ('queued','running')", (app['name'],)):
            version = json.loads(row['result'] or '{}').get('version')
            if version:
                keep.add(version)
        return keep

    def in_use(self) -> set[str]:
        """Image IDs a revision the platform may still start, or the platform itself, refers to."""
        ids: set[str] = set()
        for app in self.db.apps():
            if app['kind'] != 'runtime':
                continue
            for version in self.kept_versions(app):
                manifest = self.runtime.manifest(app['name'], version) or {}
                if str(manifest.get('image', '')).startswith('sha256:'):
                    ids.add(manifest['image'])
                built = self.db.get_setting(f"built-image:{app['name']}:{version}")
                if built and built.get('image'):
                    ids.add(built['image'])
        for image in (self.platform.settings.helper_image, self.platform.settings.builder_image):
            try:
                ids.add(self.docker.inspect_image(image)['Id'])
            except Exception:
                pass
        return ids

    def remove_recorded(self, candidates: set[str]) -> list[str]:
        """Delete platform-built images of revisions that are no longer kept."""
        if not candidates or not self.docker or not hasattr(self.docker, 'remove_image'):
            return []
        removed = []
        protected = self.in_use()  # checked again here, just before deleting
        for image in sorted(candidates - protected):
            try:
                # A manually added tag is an explicit request to keep the image.
                # Recheck immediately before deletion, even for recorded builds.
                if image in self.in_use():
                    continue
                tags = self.docker.inspect_image(image).get('RepoTags') or []
                if any(tag and tag != '<none>:<none>' for tag in tags):
                    continue
                self.docker.remove_image(image)
                removed.append(image)
            except Exception as exc:
                # 404: already gone. 409: a container still uses it, so Docker keeps it.
                if getattr(exc, 'status', None) not in (404, 409):
                    logger.warning('Image %s was not removed: %s', image[:19], exc)
        if removed:
            self.db.audit('system', 'images.cleanup', {'removed': removed})
        return removed

    # -- the owner's list on the Settings page ---------------------------------
    def report(self) -> dict:
        """Images nothing uses. Sizes are estimates: images share layers."""
        if not self.docker:
            return {'error': 'Docker is unavailable.', 'unused': []}
        protected = self.in_use()
        unused = []
        for image in self.docker.image_usage():
            tags = [t for t in (image.get('RepoTags') or []) if t and t != '<none>:<none>']
            if image.get('Containers', -1) != 0 or image['Id'] in protected:
                continue  # in use, or Docker could not tell
            size, shared = int(image.get('Size') or 0), int(image.get('SharedSize') or 0)
            unused.append({'id': image['Id'], 'tags': tags, 'bytes': size,
                           'unique_bytes': max(0, size - shared) if shared >= 0 else size,
                           'created': image.get('Created')})
        unused.sort(key=lambda item: item['unique_bytes'], reverse=True)
        untagged = [item for item in unused if not item['tags']]
        return {'unused': unused, 'untagged_count': len(untagged),
                'untagged_bytes': sum(item['unique_bytes'] for item in untagged),
                'tagged_bytes': sum(item['unique_bytes'] for item in unused if item['tags']),
                'note': 'Estimates: images share layers, so the space really freed can differ.'}

    def remove(self, image: str, actor: str) -> dict:
        """Owner action: delete one image from the unused list (checked again now)."""
        current = {item['id']: item for item in self.report()['unused']}
        if image not in current:
            raise ValueError('That image is in use now, or no longer exists. Reload the list.')
        self.docker.remove_image(image)
        self.db.audit(actor, 'images.remove', {'image': image, 'tags': current[image]['tags']})
        return {'removed': [image]}

    def remove_untagged(self, actor: str) -> dict:
        """Owner action: delete every unused image that has no name."""
        removed, kept = [], 0
        for item in self.report()['unused']:
            if item['tags']:
                continue
            try:
                self.docker.remove_image(item['id'])
                removed.append(item['id'])
            except Exception as exc:
                kept += 1
                if getattr(exc, 'status', None) not in (404, 409):
                    logger.warning('Image %s was not removed: %s', item['id'][:19], exc)
        self.db.audit(actor, 'images.cleanup', {'removed': removed, 'kept': kept, 'by': 'owner'})
        return {'removed': removed, 'kept': kept}

    def prune_host_cache(self, actor: str) -> dict:
        freed = self.docker.prune_build_cache()
        self.db.audit(actor, 'images.host_cache', {'freed_bytes': freed})
        return {'freed_bytes': freed}
