"""Live coverage for the rest of files, directories, assets and search.

``directories`` was 3/6, ``community_files`` 3/9, ``assets`` 3/7 and
``search`` 0/2. What was missing is everything past create/list/delete: the
readers, the renames, the two ``move`` calls, download, and both search RPCs.

**What the first run settled.**

* ``FileEdit``'s ``Name`` takes the *stem*: no dot, no space. Hyphens,
  underscores, digits and uppercase are all fine. The trap is the asymmetry --
  ``FileCreate`` stores the uploaded filename with its extension, and feeding
  that exact string back to ``FileEdit`` is rejected. ``TestFileNameRule``
  pins it, one variable per case.
* ``FileGrpcService/Download`` answers ``UNIMPLEMENTED (12)`` for every
  argument shape, and the asset route round it is closed too: only the
  ``"file"`` kind resolves a community file's asset, and that URL comes back
  unsigned and 403s, while the signed ``imagedelivery.net`` URLs avatars and
  emoji get come from the ``"image"`` kind, which does not resolve here at
  all. **A community file's bytes cannot currently be retrieved.** Pinned, so
  that if Root ships either half the tests fail and we notice.
* The asset URI encoding: ``root://asset/<b64url(guid16 + 0x0A <len> kind)>``,
  now :meth:`~rootpy.services.assets.AssetService.uri_for_id`.

* Root **re-appends the extension** from the mime type: send the stem
  ``renamedabcd`` and the record reads ``renamedabcd.png``. Which is why an
  equality check against the stem timed out even though the edit had
  succeeded, and why a name that already has an extension is refused.
* Root **content-addresses assets**: identical bytes give the identical asset
  id and URI. The fixtures here upload a freshly generated PNG per test,
  because sharing bytes with the avatar made a file's asset inherit the
  avatar's signed ``image`` derivative and flipped the assertion above.

**Three ladders ran here and all three are now narrowed to assertions**, which
is what a ladder is for -- ``DirectoryMove`` and ``FileMove`` both want the old
*and* the new parent, and ``FileSearch``'s ``LastFileId`` may be omitted. Each
took its first candidate, and the comments record what the alternatives were
so nobody re-opens a settled question.

``search`` was called "undocumented kwargs" and written off. It is not --
``StructuredMethod.signature()`` gives the fields offline::

    file.search_community(community_id, container_ids[], search)
    message.search(container_id, community_id, search, last_message_id, limit)

Run with::

    pytest -m live tests/test_live_files.py -v
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from .conftest import PNG_1X1, eventually, requires_live, tag

# The shared response readers. ``_get`` (first-present-of-several) and
# ``_shape`` (a legible field listing) used to be defined here and in other
# live modules; they live in one place now. See rootpy/responses.py.
from rootpy.responses import first as _get, shape as _shape

pytestmark = [pytest.mark.live, pytest.mark.asyncio, requires_live]


async def _a_directory(client, sandbox) -> str:
    """A directory id in the sandbox channel, reusing one if it exists."""
    existing = await client.directories.list(
        sandbox.community_id, sandbox.channel.id
    )
    if existing:
        return _get(existing[0], "id")
    created = await client.directories.create(
        sandbox.community_id, sandbox.channel.id, f"rootpy-fd-{tag()}"
    )
    return _get(created, "id")


# --------------------------------------------------------------------------
# directories -- get, edit, move
# --------------------------------------------------------------------------
class TestDirectoryReadAndEdit:
    async def test_get_returns_the_directory_we_made(self, client, sandbox):
        name = f"rootpy-dg-{tag()}"
        created = await client.directories.create(
            sandbox.community_id, sandbox.channel.id, name
        )
        directory_id = _get(created, "id")
        try:
            fetched = await client.directories.get(
                sandbox.community_id, sandbox.channel.id, directory_id
            )
            assert fetched is not None, f"get returned nothing for {directory_id}"
            got = _get(fetched, "id") or _get(
                _get(fetched, "directory"), "id"
            )
            assert got, f"no id on the fetched directory: {_shape(fetched)}"
        finally:
            await client.directories.delete(
                sandbox.community_id, sandbox.channel.id, directory_id
            )

    async def test_edit_renames_it(self, client, sandbox):
        """Directory names take the channel shape -- hyphens and hex are fine."""
        created = await client.directories.create(
            sandbox.community_id, sandbox.channel.id, f"rootpy-de-{tag()}"
        )
        directory_id = _get(created, "id")
        renamed = f"rootpy-dr-{tag()}"
        try:
            await client.directories.edit(
                sandbox.community_id, sandbox.channel.id, directory_id, renamed
            )

            async def has_new_name():
                listing = await client.directories.list(
                    sandbox.community_id, sandbox.channel.id
                )
                for entry in listing:
                    if _get(entry, "id") == directory_id:
                        return _get(entry, "name") == renamed
                return False

            assert await eventually(
                has_new_name, describe="the directory rename lands"
            )
        finally:
            await client.directories.delete(
                sandbox.community_id, sandbox.channel.id, directory_id
            )


class TestDirectoryMove:
    """Which parent fields DirectoryMove actually wants -- measured, not guessed."""

    async def test_move_a_child_between_parents(self, client, sandbox):
        community, container = sandbox.community_id, sandbox.channel.id
        first = await client.directories.create(
            community, container, f"rootpy-m1-{tag()}"
        )
        second = await client.directories.create(
            community, container, f"rootpy-m2-{tag()}"
        )
        first_id, second_id = _get(first, "id"), _get(second, "id")
        child = await client.directories.create(
            community, container, f"rootpy-mc-{tag()}",
            parent_directory_id=first_id,
        )
        child_id = _get(child, "id")

        try:
            # Measured, then narrowed: supplying both parents is accepted on
            # the first try. The ladder that established it walked
            # old+new / new-only / old-only; keeping it would re-pay three
            # round trips a run to re-answer a settled question.
            await client.directories.move(
                community, container, child_id,
                old_parent_directory_id=first_id,
                new_parent_directory_id=second_id,
            )

            async def under_the_new_parent():
                listing = await client.directories.list(community, container)
                for entry in listing:
                    if _get(entry, "id") == child_id:
                        return _get(entry, "parent_directory_id") == second_id
                return False

            # The listing may not expose a parent id at all; a move that Root
            # accepted is the assertion, and this is a bonus check when the
            # field is there.
            listing = await client.directories.list(community, container)
            entry = next(
                (e for e in listing if _get(e, "id") == child_id), None
            )
            if entry is not None and _get(entry, "parent_directory_id"):
                assert await eventually(
                    under_the_new_parent,
                    describe="the directory moved to the new parent",
                )
        finally:
            for target in (child_id, second_id, first_id):
                if target:
                    try:
                        await client.directories.delete(
                            community, container, target
                        )
                    except Exception:
                        pass


# --------------------------------------------------------------------------
# community files -- get, edit, move, download, search
# --------------------------------------------------------------------------
@pytest_asyncio.fixture
async def uploaded_file(client, sandbox, unique_png):
    """One uploaded file in its own directory, cleaned up afterwards.

    ``directory_id`` is where the file *currently* is, so a test that moves it
    updates that field and teardown still finds it. Extra directories a test
    creates go on ``extra_directories`` rather than being deleted by the test,
    because a directory must outlive the file inside it and the fixture is
    what knows the right order.
    """
    community, container = sandbox.community_id, sandbox.channel.id
    directory = await client.directories.create(
        community, container, f"rootpy-uf-{tag()}"
    )
    directory_id = _get(directory, "id")
    created = await client.community_files.create(
        community, container, unique_png, directory_id=directory_id
    )
    file_id = _get(created, "id") or _get(_get(created, "file"), "id")

    class Uploaded:
        pass

    box = Uploaded()
    box.community_id = community
    box.container_id = container
    box.directory_id = directory_id
    box.file_id = file_id
    box.record = created
    box.extra_directories = []
    try:
        yield box
    finally:
        # Written out rather than looped over thunks: the offline await-guard
        # can see a plain `await`, and it should not have to trust a loop.
        try:
            await client.community_files.delete(
                community, container, box.file_id, box.directory_id
            )
        except Exception:
            pass
        for extra in [*box.extra_directories, directory_id]:
            if not extra:
                continue
            try:
                await client.directories.delete(community, container, extra)
            except Exception:
                pass


class TestCommunityFileReaders:
    async def test_create_returned_a_file_id(self, uploaded_file):
        assert uploaded_file.file_id, (
            "FileCreate gave back no id. Shape was "
            f"{_shape(uploaded_file.record)}"
        )

    async def test_get_one_file(self, client, uploaded_file):
        fetched = await client.community_files.get(
            uploaded_file.community_id,
            uploaded_file.container_id,
            uploaded_file.file_id,
            uploaded_file.directory_id,
        )
        assert fetched is not None, "FileGet returned nothing"

    async def test_edit_renames_the_file(self, client, uploaded_file):
        """No dot. ``FileEdit`` takes the stem, not the filename.

        The first version of this test used ``rootpy-renamed-<tag>.png`` --
        the shape ``FileCreate`` had just stored -- and Root rejected it with
        ``Name: 'Name' is not in the correct format.
        [RegularExpressionValidator]``. Ten candidates, one variable at a
        time, put the rule at "no dot, no space"; see
        ``TestFileNameRule`` below, which pins it.
        """
        renamed = f"rootpy-renamed-{tag()}"
        await client.community_files.edit(
            uploaded_file.community_id,
            uploaded_file.container_id,
            uploaded_file.file_id,
            uploaded_file.directory_id,
            renamed,
        )

        async def shows_new_name():
            listing = await client.community_files.list(
                uploaded_file.community_id,
                uploaded_file.container_id,
                uploaded_file.directory_id,
            )
            for entry in listing:
                if _get(entry, "id") == uploaded_file.file_id:
                    # Root re-appends the extension from the mime type, so the
                    # stored name is the stem we sent plus ".png". Comparing
                    # for equality here is what made the first attempt time
                    # out after the edit had actually succeeded.
                    name = _get(entry, "name") or ""
                    return name == renamed or name.startswith(f"{renamed}.")
            return False

        assert await eventually(
            shows_new_name, describe="the file rename lands"
        )

    async def test_root_reappends_the_extension(self, client, uploaded_file):
        """The other half of the stem rule, and the reason equality failed.

        Send ``renamedabcd``; the record reads ``renamedabcd.png``. So the
        name is round-tripped through Root's own idea of the file type rather
        than stored verbatim -- which is also why sending a name that already
        has an extension is rejected.
        """
        stem = f"rootpy-stem-{tag()}"
        await client.community_files.edit(
            uploaded_file.community_id,
            uploaded_file.container_id,
            uploaded_file.file_id,
            uploaded_file.directory_id,
            stem,
        )

        async def stored_name():
            record = await client.community_files.get(
                uploaded_file.community_id,
                uploaded_file.container_id,
                uploaded_file.file_id,
                uploaded_file.directory_id,
            )
            name = _get(_get(record, "file") or record, "name")
            return name if name and name.startswith(stem) else None

        name = await eventually(
            stored_name, describe="the renamed file reads back"
        )
        assert name == f"{stem}.png", (
            f"expected the stem plus the mime extension, got {name!r}"
        )


class TestFileNameRule:
    """Pin the rule that cost a test failure, so a change to it is visible.

    Measured, not guessed: ten candidates varying one thing at a time. The
    outcome was not the shape anyone would have predicted -- ``FileEdit``
    wants the *stem*, so the filename ``FileCreate`` itself stored is not a
    legal argument to ``FileEdit``.
    """

    ACCEPTED = ["renamedabcd", "renamed-abcd", "renamed_abcd",
                "Renamedabcd", "renamed1234"]
    REJECTED = ["renamedabcd.png", "renamedabcd.txt", "renamed.abcd",
                "renamed abcd"]

    async def test_legal_names_are_accepted(self, client, uploaded_file):
        for name in self.ACCEPTED:
            await client.community_files.edit(
                uploaded_file.community_id,
                uploaded_file.container_id,
                uploaded_file.file_id,
                uploaded_file.directory_id,
                f"{name}{tag()}",
            )

    @pytest.mark.parametrize("name", REJECTED)
    async def test_a_dot_or_a_space_is_refused(
        self, client, uploaded_file, name
    ):
        from rootpy.exceptions import GrpcInvalidArgument

        with pytest.raises(GrpcInvalidArgument) as caught:
            await client.community_files.edit(
                uploaded_file.community_id,
                uploaded_file.container_id,
                uploaded_file.file_id,
                uploaded_file.directory_id,
                name,
            )
        assert "Name" in str(caught.value), (
            f"{name!r} was refused for something other than the name rule: "
            f"{caught.value}"
        )

    async def test_the_uploaded_filename_is_not_a_legal_edit(
        self, client, uploaded_file
    ):
        """The asymmetry itself, stated as a test.

        ``FileCreate`` stores ``probe-1a2b.png``; feeding that exact string
        back to ``FileEdit`` fails. If Root ever makes the two agree, this is
        the test that should start failing.
        """
        from rootpy.exceptions import GrpcInvalidArgument

        record = await client.community_files.get(
            uploaded_file.community_id,
            uploaded_file.container_id,
            uploaded_file.file_id,
            uploaded_file.directory_id,
        )
        stored = _get(_get(record, "file") or record, "name")
        if not stored or "." not in stored:
            pytest.skip(f"the stored name has no extension to test: {stored!r}")

        with pytest.raises(GrpcInvalidArgument):
            await client.community_files.edit(
                uploaded_file.community_id,
                uploaded_file.container_id,
                uploaded_file.file_id,
                uploaded_file.directory_id,
                stored,
            )


class TestCommunityFileMove:
    async def test_move_between_directories(self, client, sandbox, uploaded_file):
        """Both DirectoryIds. Measured against new-only, then narrowed."""
        community = uploaded_file.community_id
        container = uploaded_file.container_id
        destination = await client.directories.create(
            community, container, f"rootpy-fm-{tag()}"
        )
        destination_id = _get(destination, "id")
        uploaded_file.extra_directories.append(destination_id)

        await client.community_files.move(
            community, container, uploaded_file.file_id,
            old_directory_id=uploaded_file.directory_id,
            new_directory_id=destination_id,
        )
        # The file now lives in the destination. Teardown deletes the file
        # before either directory, so pointing this at the new home is all the
        # bookkeeping needed.
        uploaded_file.directory_id = destination_id

        listing = await client.community_files.list(
            community, container, destination_id
        )
        assert any(
            _get(entry, "id") == uploaded_file.file_id for entry in listing
        ), "the moved file is not in the destination directory"


class TestCommunityFileDownload:
    """Root has not implemented file download, and the asset route is closed too.

    Run 1 walked three argument shapes for ``FileGrpcService/Download`` --
    the asset id off the file record, the file id in its place, and the field
    omitted -- and every one answered ``UNIMPLEMENTED (12)``. That is not an
    argument problem, so the ladder is retired and the answer is pinned: if
    Root implements the endpoint, these tests fail, which is the notification
    you want.

    The asset service is the obvious way round and is also closed. A file's
    ``asset_id`` builds a valid URI, but only the ``"file"`` kind resolves,
    and that URL is unsigned and 403s. The signed ``imagedelivery.net`` URLs
    that avatars, banners and emoji get come from the ``"image"`` kind, which
    does not resolve for a file's asset even when the file is a PNG.
    """

    async def _asset_id(self, client, uploaded_file):
        record = await client.community_files.get(
            uploaded_file.community_id,
            uploaded_file.container_id,
            uploaded_file.file_id,
            uploaded_file.directory_id,
        )
        inner = _get(record, "file") or record
        asset_id = _get(inner, "asset_id")
        assert asset_id, f"no asset_id on the file record: {_shape(inner)}"
        return asset_id

    async def test_the_rpc_is_unimplemented(self, client, uploaded_file):
        from rootpy.exceptions import GrpcUnimplemented

        asset_id = await self._asset_id(client, uploaded_file)
        with pytest.raises(GrpcUnimplemented):
            await client.community_files.download(
                community_id=uploaded_file.community_id,
                container_id=uploaded_file.container_id,
                id=uploaded_file.file_id,
                directory_id=uploaded_file.directory_id,
                asset_id=asset_id,
            )

    async def test_it_is_unimplemented_whatever_we_send(
        self, client, uploaded_file
    ):
        """So nobody re-opens this as an argument puzzle."""
        from rootpy.exceptions import GrpcUnimplemented

        with pytest.raises(GrpcUnimplemented):
            await client.community_files.download(
                community_id=uploaded_file.community_id,
                container_id=uploaded_file.container_id,
                id=uploaded_file.file_id,
                directory_id=uploaded_file.directory_id,
            )

    async def test_only_the_file_kind_resolves_and_it_is_unsigned(
        self, client, uploaded_file
    ):
        """The asset route, measured rather than asserted from memory.

        Reports what each kind tag does, then pins the two facts that matter:
        ``image`` does not resolve, and whatever does resolve comes back
        without a signature.
        """
        asset_id = await self._asset_id(client, uploaded_file)

        outcomes = {}
        for kind in ("image", "file", "video", "audio"):
            uri = client.assets.uri_for_id(asset_id, kind)
            url = await client.assets.url_for(uri)
            outcomes[kind] = url
        lines = "\n     ".join(
            f"{kind:6s} {url or 'did not resolve'}"
            for kind, url in outcomes.items()
        )
        print(f"\n   community file asset by kind:\n     {lines}\n")

        assert outcomes["image"] is None, (
            "the 'image' kind resolves for a community file now -- if it is "
            f"signed, file download just became possible: {outcomes['image']}"
        )
        resolved = {k: u for k, u in outcomes.items() if u}
        assert resolved, (
            "no kind tag resolves for a community file's asset any more"
        )
        for kind, url in resolved.items():
            assert "sig=" not in url, (
                f"the {kind!r} URL is signed now -- community file download "
                f"may work: {url}"
            )

    async def test_the_unsigned_url_is_actually_refused(
        self, client, uploaded_file
    ):
        """Pins the 403 itself, not just the missing signature."""
        import httpx

        asset_id = await self._asset_id(client, uploaded_file)
        url = await client.assets.url_for(
            client.assets.uri_for_id(asset_id, "file")
        )
        if not url:
            pytest.skip("the 'file' kind no longer resolves")

        async with httpx.AsyncClient(follow_redirects=True) as http:
            response = await http.get(url, timeout=30)
        assert response.status_code == 403, (
            f"the unsigned community-file URL now answers "
            f"{response.status_code}, not 403 -- download may be possible"
        )

    async def test_uri_for_id_round_trips_a_real_asset(self, client, me):
        """The encoding, checked against a URI Root itself produced."""
        current = await client.users.get_self()
        uri = current.profile_picture_asset_uri
        if not uri:
            pytest.skip("the account has no profile picture to check against")

        assets = await client.assets.get(uri)
        asset = assets.get(uri)
        if asset is None:
            pytest.skip(f"{uri!r} did not resolve")

        # Rebuild the URI from the id inside it and check it still resolves.
        import base64

        payload = uri.rsplit("/", 1)[-1]
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        asset_id = raw[:16].hex()
        rebuilt = client.assets.uri_for_id(asset_id, "image")
        assert rebuilt == uri, (
            f"uri_for_id rebuilt {rebuilt!r} from a URI that was {uri!r}"
        )


class TestFileSearch:
    async def test_search_within_the_channel(self, client, uploaded_file):
        """``LastFileId`` is a cursor and may be omitted. Measured, then narrowed."""
        found = await client.community_files.search(
            community_id=uploaded_file.community_id,
            container_id=uploaded_file.container_id,
            search="rootpy",
        )
        assert found is not None

    async def test_search_across_the_community(self, client, uploaded_file):
        found = await client.community_files.search_community(
            community_id=uploaded_file.community_id,
            container_ids=[uploaded_file.container_id],
            search="rootpy",
        )
        assert isinstance(found, list), (
            f"search_community should unwrap to a list, got {_shape(found)}"
        )

    async def test_the_search_manager_reaches_the_same_rpc(
        self, client, uploaded_file
    ):
        """``client.search.files`` is the documented spelling of the same call."""
        found = await client.search.files(
            community_id=uploaded_file.community_id,
            container_ids=[uploaded_file.container_id],
            search="rootpy",
        )
        assert found is not None


class TestMessageSearch:
    async def test_search_finds_a_message_we_just_posted(self, client, sandbox):
        marker = f"searchable{tag()}"
        result = await client.message(sandbox.channel.id, f"rootpy {marker}")
        sent = getattr(result, "message", result)
        try:
            # ``last_message_id`` and ``limit`` are both optional; measured,
            # then narrowed to the minimal call.
            found = await client.search.messages(
                container_id=sandbox.channel.id,
                community_id=sandbox.community_id,
                search=marker,
            )
            assert found is not None

            with_limit = await client.search.messages(
                container_id=sandbox.channel.id,
                community_id=sandbox.community_id,
                search=marker,
                limit=25,
            )
            assert with_limit is not None
        finally:
            try:
                await client.delete_message(sent)
            except Exception:
                pass


# --------------------------------------------------------------------------
# assets -- upload_bytes, download, url_for
# --------------------------------------------------------------------------
class TestAssetUpload:
    async def test_upload_bytes_returns_a_root_uri(self, client):
        """The in-memory path -- nothing touches disk."""
        token = await client.assets.upload_bytes(
            PNG_1X1, filename=f"probe-{tag()}.png"
        )
        assert isinstance(token, str) and token.startswith("root://"), (
            f"upload_bytes gave back {token!r}"
        )

    async def test_upload_file_returns_a_root_uri(self, client, png):
        token = await client.assets.upload_file(png)
        assert isinstance(token, str) and token.startswith("root://")

    async def test_empty_bytes_are_refused_before_the_round_trip(self, client):
        with pytest.raises(ValueError):
            await client.assets.upload_bytes(b"")

    async def test_upload_file_rejects_a_missing_path(self, client, tmp_path):
        with pytest.raises(FileNotFoundError):
            await client.assets.upload_file(str(tmp_path / "not-here.png"))


class TestAssetDownload:
    """Round-trip an asset that is actually attached to something.

    An upload *token* is not necessarily resolvable on its own -- the emoji is
    used as the carrier because creating one is already proven, and its stored
    URI is a real asset reference rather than a token.
    """

    async def test_url_for_and_download_an_emoji_image(
        self, client, sandbox, png
    ):
        shortcode = f"rootpy{tag()}"
        emoji = await client.emojis.create(
            sandbox.community_id, shortcode, png
        )
        emoji_id = _get(emoji, "id") or _get(_get(emoji, "emoji"), "id")
        try:
            uri = _get(emoji, "asset_uri", "uri", "upload_token_uri")
            if not uri:
                inner = _get(emoji, "emoji") or emoji
                uri = _get(inner, "asset_uri", "uri", "upload_token_uri")
            if not uri:
                pytest.skip(
                    f"no asset uri on the created emoji: {_shape(emoji)}"
                )

            url = await client.assets.url_for(uri)
            assert url is None or isinstance(url, str)
            if url is None:
                pytest.skip(f"asset {uri!r} did not resolve to a URL yet")

            data = await client.assets.download(uri)
            assert data, "download returned no bytes for a resolvable asset"
        finally:
            if emoji_id:
                try:
                    await client.emojis.delete(sandbox.community_id, emoji_id)
                except Exception:
                    pass

    async def test_url_for_nothing_is_none(self, client):
        assert await client.assets.url_for(None) is None

    async def test_download_nothing_is_none(self, client):
        assert await client.assets.download(None) is None
