'use strict';

const fs = require('fs');
const https = require('https');

const [url, destination, requestedConcurrency = '8'] = process.argv.slice(2);
const concurrency = Number(requestedConcurrency);
if (!url || !destination || !Number.isInteger(concurrency) || concurrency < 1 || concurrency > 16) {
  console.error('Usage: node download_https_parallel.js <https-url> <destination> [concurrency 1..16]');
  process.exit(2);
}

const maximumAttempts = 12;
const agent = new https.Agent({ keepAlive: true, maxSockets: concurrency });

const requestRange = (currentUrl, start, end, validators, redirectCount = 0) => new Promise((resolve, reject) => {
  const headers = { Range: `bytes=${start}-${end}` };
  if (validators.etag) headers['If-Range'] = validators.etag;
  else if (validators.lastModified) headers['If-Range'] = validators.lastModified;
  const request = https.get(currentUrl, { headers, agent }, (response) => {
    if ([301, 302, 303, 307, 308].includes(response.statusCode)) {
      response.resume();
      if (!response.headers.location || redirectCount >= 5) {
        reject(new Error('HTTPS download exceeded the redirect limit or omitted Location.'));
        return;
      }
      const redirected = new URL(response.headers.location, currentUrl);
      if (redirected.protocol !== 'https:') {
        reject(new Error('HTTPS download refused a non-HTTPS redirect.'));
        return;
      }
      requestRange(redirected.href, start, end, validators, redirectCount + 1).then(resolve, reject);
      return;
    }
    resolve(response);
  });
  request.setTimeout(60000, () => request.destroy(new Error('HTTPS request timed out')));
  request.on('error', reject);
});

const probe = async () => {
  const response = await requestRange(url, 0, 0, {});
  const contentRange = String(response.headers['content-range'] || '');
  const match = contentRange.match(/^bytes 0-0\/(\d+)$/);
  response.resume();
  if (response.statusCode !== 206 || !match) {
    throw new Error(`Server does not expose a stable byte-range length (HTTP ${response.statusCode}).`);
  }
  return {
    total: Number(match[1]),
    etag: String(response.headers.etag || ''),
    lastModified: String(response.headers['last-modified'] || ''),
  };
};

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

const downloadPart = async (index, start, end, validators, partsDirectory) => {
  const partPath = `${partsDirectory}/part-${String(index).padStart(3, '0')}`;
  const expectedSize = end - start + 1;
  for (let attempt = 1; attempt <= maximumAttempts; attempt += 1) {
    let existing = fs.existsSync(partPath) ? fs.statSync(partPath).size : 0;
    if (existing === expectedSize) return partPath;
    if (existing > expectedSize) {
      fs.truncateSync(partPath, 0);
      existing = 0;
    }
    try {
      const rangeStart = start + existing;
      const response = await requestRange(url, rangeStart, end, validators);
      const contentRange = String(response.headers['content-range'] || '');
      const expectedRange = `bytes ${rangeStart}-${end}/${validators.total}`;
      if (response.statusCode !== 206 || contentRange !== expectedRange) {
        response.resume();
        throw new Error(`unexpected range response HTTP ${response.statusCode}: ${contentRange}`);
      }
      await new Promise((resolve, reject) => {
        const output = fs.createWriteStream(partPath, { flags: existing ? 'a' : 'w' });
        let settled = false;
        const fail = (error) => {
          if (settled) return;
          settled = true;
          output.destroy();
          reject(error);
        };
        response.on('aborted', () => fail(new Error('response aborted')));
        response.on('error', fail);
        output.on('error', fail);
        output.on('finish', () => {
          if (settled) return;
          settled = true;
          output.close(resolve);
        });
        response.pipe(output);
      });
      if (fs.statSync(partPath).size === expectedSize) {
        console.error(`Downloaded segment ${index + 1}/${concurrency}.`);
        return partPath;
      }
      throw new Error('segment ended before the declared range length');
    } catch (error) {
      if (attempt === maximumAttempts) throw error;
      console.error(`Segment ${index + 1} attempt ${attempt} failed: ${error.message}; resuming.`);
      await delay(Math.min(attempt, 5) * 1000);
    }
  }
  throw new Error(`Segment ${index + 1} exhausted retries.`);
};

const assemble = (parts, outputPath, expectedSize) => {
  const output = fs.openSync(outputPath, 'w');
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  try {
    for (const part of parts) {
      const input = fs.openSync(part, 'r');
      try {
        let bytesRead;
        while ((bytesRead = fs.readSync(input, buffer, 0, buffer.length, null)) > 0) {
          fs.writeSync(output, buffer, 0, bytesRead);
        }
      } finally {
        fs.closeSync(input);
      }
    }
    fs.fsyncSync(output);
  } finally {
    fs.closeSync(output);
  }
  if (fs.statSync(outputPath).size !== expectedSize) {
    throw new Error('Assembled download size does not match the server length.');
  }
};

(async () => {
  const validators = await probe();
  if (!Number.isSafeInteger(validators.total) || validators.total <= 0) {
    throw new Error('Invalid HTTPS content length.');
  }
  const partsDirectory = `${destination}.parts`;
  fs.mkdirSync(partsDirectory, { recursive: true });
  const segmentSize = Math.ceil(validators.total / concurrency);
  const ranges = Array.from({ length: concurrency }, (_, index) => {
    const start = index * segmentSize;
    const end = Math.min(validators.total - 1, start + segmentSize - 1);
    return { index, start, end };
  }).filter(({ start }) => start < validators.total);
  const parts = await Promise.all(ranges.map(({ index, start, end }) =>
    downloadPart(index, start, end, { ...validators, total: validators.total }, partsDirectory)));
  assemble(parts, destination, validators.total);
  fs.rmSync(partsDirectory, { recursive: true, force: true });
  console.error(`Completed ${validators.total} bytes with ${ranges.length} verified ranges.`);
})().catch((error) => {
  console.error(error.stack || error.message);
  process.exitCode = 1;
});
