import {readFileSync} from 'node:fs';
import {IDBFactory, IDBKeyRange} from 'fake-indexeddb';
import {TextWriter, Uint8ArrayReader, ZipReader} from './lib/zip.js';
import {DictionaryDatabase} from './js/dictionary/dictionary-database.js';
import {DictionaryImporter} from './js/dictionary/dictionary-importer.js';

const archivePath = process.argv[2];
if (!archivePath) throw new Error('archive path required');

class WorkerStub {
    addEventListener() {}
    terminate() {}
    postMessage() {}
}

class MediaLoader {
    async getImageDetails(content) {
        return {content, width: 100, height: 100};
    }
}

globalThis.indexedDB = new IDBFactory();
globalThis.IDBKeyRange = IDBKeyRange;
globalThis.self = {constructor: {name: 'Window'}};
globalThis.Worker = WorkerStub;

const source = readFileSync(archivePath);
const archive = source.buffer.slice(source.byteOffset, source.byteOffset + source.byteLength);
const zipReader = new ZipReader(new Uint8ArrayReader(new Uint8Array(archive)));
const zipEntries = await zipReader.getEntries();
const termBankEntry = zipEntries.find((entry) => /^term_bank_\d+\.json$/.test(entry.filename));
if (!termBankEntry || typeof termBankEntry.getData !== 'function') {
    throw new Error('dictionary has no readable term bank');
}
const firstBank = JSON.parse(await termBankEntry.getData(new TextWriter()));
const sampleTerm = firstBank[0]?.[0];
if (typeof sampleTerm !== 'string' || sampleTerm.length === 0) {
    throw new Error('dictionary has no sample term');
}
await zipReader.close();

const database = new DictionaryDatabase();
await database.prepare();
try {
    const importer = new DictionaryImporter(new MediaLoader());
    const {errors, result} = await importer.importDictionary(database, archive, {
        prefixWildcardsSupported: true,
        yomitanVersion: process.env.YOMITAN_VERSION ?? 'compatibility-test',
    });
    if (errors.length > 0 || result === null || !result.importSuccess) {
        throw new Error(JSON.stringify({
            errors: errors.map((error) => error.message),
            result,
        }));
    }

    const matches = await database.findTermsBulk(
        [sampleTerm],
        new Set([result.title]),
        'exact',
    );
    if (!matches.some((entry) => entry.term === sampleTerm)) {
        throw new Error(`lookup failed for sample term ${sampleTerm}`);
    }

    console.log(JSON.stringify({
        archive: archivePath,
        title: result.title,
        revision: result.revision,
        counts: result.counts,
        importSuccess: result.importSuccess,
        sampleTerm,
        lookupMatches: matches.length,
    }));
} finally {
    await database.close();
}
