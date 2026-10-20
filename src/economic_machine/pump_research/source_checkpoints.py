"""Batch portable source checkpoints; retirement always follows a durable receipt."""
from pathlib import Path

from .research_cloud import archive_file, load_reference, retire_file
from .store import atomic


def migrate_sources(folder, epoch, source, portable_source, portable_import,
                    scope, cloud, report, *, checkpoint_chunks=16):
    if type(checkpoint_chunks) is not int or not 1 <= checkpoint_chunks <= 32:
        raise ValueError('CLOUD_SOURCE_CHECKPOINT_BOUND')
    folder = Path(folder)
    (folder / 'cloud-chunks').mkdir(exist_ok=True)
    progress_path = folder / 'cloud-progress-catalog.json'
    references, pending = [], []
    retired = durable = 0
    chunks = source['chunks']
    for i, row in enumerate(chunks):
        scope.guard()
        if Path(row['path']).name != row['path']:
            raise ValueError('CLOUD_CHUNK_PATH')
        path = folder / 'chunks' / row['path']
        reference = folder / 'cloud-chunks' / (row['path'] + '.json')
        if path.exists():
            archive_file(cloud, path, reference, row)
        else:
            load_reference(reference, row)
        references.append(dict(path=str(reference.relative_to(folder)),
                               **cloud.upload(reference)))
        pending.append((path, reference, row))
        if len(pending) == checkpoint_chunks or i + 1 == len(chunks):
            # Identical v1 document shape preserves old recovery/readers.
            atomic(progress_path, dict(schema='economic-machine.research-cloud-progress.v1',
                epoch=epoch, source_manifest=portable_source, import_receipt=portable_import,
                references=references, completed_chunks=i + 1, total_chunks=len(chunks),
                whole_epoch_cloud_ready=False))
            receipt = cloud.upload(progress_path)
            atomic(folder / 'cloud-progress-catalog.receipt.json', receipt)
            # No file in this window is retired before the entire window has
            # a portable, fully read-back catalogue and a durable local receipt.
            for original, ref, expected in pending:
                scope.guard()
                retired += retire_file(cloud, original, ref, expected)
            pending.clear()
            durable = i + 1
        report('ARCHIVING_COMPRESSED_SOURCE', epoch=epoch,
               chunks_verified=i + 1, chunks_total=len(chunks),
               durable_source_checkpoint_chunks=durable,
               checkpoint_chunks=checkpoint_chunks,
               duplicate_bytes_retired_this_run=retired)
    return retired
