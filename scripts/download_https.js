'use strict';

const fs = require('fs');
const https = require('https');

const [url, destination] = process.argv.slice(2);
if (!url || !destination) {
  console.error('Usage: node download_https.js <https-url> <destination>');
  process.exit(2);
}

const maximumAttempts = 12;
let attempt = 0;
let completed = false;

const finishWithError = (message) => {
  if (completed) return;
  completed = true;
  console.error(message);
  process.exitCode = 1;
};

const retry = (message) => {
  if (completed) return;
  if (attempt >= maximumAttempts) {
    finishWithError(`${message}; exhausted ${maximumAttempts} attempts`);
    return;
  }
  const delayMs = Math.min(attempt, 5) * 1000;
  console.error(`${message}; retrying from the saved byte offset (${attempt}/${maximumAttempts})`);
  setTimeout(() => download(url), delayMs);
};

const download = (currentUrl, redirectCount = 0) => {
  if (completed) return;
  if (redirectCount === 0) attempt += 1;
  const offset = fs.existsSync(destination) ? fs.statSync(destination).size : 0;
  const headers = offset > 0 ? { Range: `bytes=${offset}-` } : {};
  const request = https.get(currentUrl, { headers }, (response) => {
    if ([301, 302, 303, 307, 308].includes(response.statusCode)) {
      response.resume();
      if (!response.headers.location || redirectCount >= 5) {
        finishWithError('HTTPS download exceeded the redirect limit or omitted Location.');
        return;
      }
      const redirectedUrl = new URL(response.headers.location, currentUrl);
      if (redirectedUrl.protocol !== 'https:') {
        finishWithError('HTTPS download refused a non-HTTPS redirect.');
        return;
      }
      download(redirectedUrl.href, redirectCount + 1);
      return;
    }
    if (response.statusCode === 416 && offset > 0) {
      response.resume();
      const contentRange = String(response.headers['content-range'] || '');
      const totalMatch = contentRange.match(/^bytes \*\/(\d+)$/);
      if (totalMatch && Number(totalMatch[1]) === offset) {
        completed = true;
        return;
      }
    }
    const append = offset > 0 && response.statusCode === 206;
    if (response.statusCode !== 200 && !append) {
      response.resume();
      retry(`HTTP ${response.statusCode}`);
      return;
    }

    const startingSize = append ? offset : 0;
    const expectedChunkSize = Number(response.headers['content-length'] || 0);
    const expectedFinalSize = expectedChunkSize > 0 ? startingSize + expectedChunkSize : 0;
    const output = fs.createWriteStream(destination, { flags: append ? 'a' : 'w' });
    let settled = false;
    const retryOnce = (message) => {
      if (settled || completed) return;
      settled = true;
      output.once('close', () => retry(message));
      output.destroy();
    };
    response.on('aborted', () => retryOnce('HTTPS response was aborted'));
    response.on('error', (error) => retryOnce(error.message));
    output.on('error', (error) => retryOnce(error.message));
    output.on('finish', () => {
      if (settled || completed) return;
      settled = true;
      output.close(() => {
        const finalSize = fs.statSync(destination).size;
        if (expectedFinalSize > 0 && finalSize !== expectedFinalSize) {
          retry(`Downloaded size ${finalSize} did not reach expected size ${expectedFinalSize}`);
          return;
        }
        completed = true;
      });
    });
    response.pipe(output);
  });
  request.setTimeout(60000, () => request.destroy(new Error('HTTPS request timed out')));
  request.on('error', (error) => retry(error.message));
};

download(url);
