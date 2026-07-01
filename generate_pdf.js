#!/usr/bin/env node
/**
 * generate_pdf.js — print carrier report pages to PDF via headless Edge
 *
 * Single:  node generate_pdf.js <DOT> [MM/DD/YYYY] [OUTPUT_FOLDER]
 * Batch:   node generate_pdf.js --batch <DOT1,DOT2,...> [MM/DD/YYYY] [OUTPUT_FOLDER]
 *
 * Examples:
 *   node generate_pdf.js 204814
 *   node generate_pdf.js 204814 01/01/2026
 *   node generate_pdf.js --batch 204814,123456,789012 01/05/2026 "C:\path\to\folder"
 *
 * Requires the carrier-portal dev server running at http://localhost:3000
 */

import puppeteer from "puppeteer";
import { mkdirSync, existsSync, readFileSync } from "fs";
import { resolve, dirname } from "path";
import { createHash } from "crypto";
import { fileURLToPath } from "url";
import { createConnection } from "net";

const EDGE_PATH = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const CONCURRENCY = 4; // parallel Edge instances

const __dirname = dirname(fileURLToPath(import.meta.url));
const BASE_URL = "http://localhost:3000";
const DEFAULT_OUTPUT_DIR = "C:\\Users\\chong\\OneDrive\\Documents\\Desktop\\MISC PROJECT\\US DIRECTORY\\CARRIER INTELLIGENT REPORT\\5 Jun 26 - CARRIER PORTAL\\carrier-reports";

function getAuthCookieValue() {
  const envPath = resolve(__dirname, "../carrier-portal/.env.local");
  const env = readFileSync(envPath, "utf8");
  const match = env.match(/^SITE_PASSWORD=(.+)$/m);
  if (!match) throw new Error("SITE_PASSWORD not found in carrier-portal/.env.local");
  return createHash("sha256").update(match[1].trim()).digest("hex");
}

function isPortalRunning() {
  return new Promise((resolve) => {
    const sock = createConnection({ host: "localhost", port: 3000 });
    sock.once("connect", () => { sock.destroy(); resolve(true); });
    sock.once("error", () => resolve(false));
    setTimeout(() => { sock.destroy(); resolve(false); }, 3000);
  });
}

function parseAccidentDate(input) {
  const m = input.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (!m) throw new Error(`Invalid date format "${input}". Use MM/DD/YYYY.`);
  const [, mm, dd, yyyy] = m;
  return `${yyyy}-${mm.padStart(2, "0")}-${dd.padStart(2, "0")}`;
}

async function generatePdf(dot, accidentDateRaw, outDir, authCookieValue) {
  const pageUrl = `${BASE_URL}/carrier/${dot}`;
  const dateSuffix = accidentDateRaw
    ? `_accident_${accidentDateRaw.replace(/\//g, "-")}`
    : "";
  const outFile = `${outDir}/DOT_${dot}${dateSuffix}.pdf`;

  const browser = await puppeteer.launch({
    executablePath: EDGE_PATH,
    headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  });

  try {
    const page = await browser.newPage();

    await page.setCookie({
      name: "cc_auth",
      value: authCookieValue,
      domain: "localhost",
      path: "/",
      httpOnly: true,
    });

    await page.goto(pageUrl, { waitUntil: "networkidle0", timeout: 60000 });

    if (accidentDateRaw) {
      const htmlDate = parseAccidentDate(accidentDateRaw);
      await page.evaluate((dateValue) => {
        const input = document.querySelector('input[type="date"]');
        if (!input) throw new Error("Date input not found on page");
        const setter = Object.getOwnPropertyDescriptor(
          window.HTMLInputElement.prototype, "value"
        ).set;
        setter.call(input, dateValue);
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
      }, htmlDate);
      await new Promise((r) => setTimeout(r, 2000));
    }

    await new Promise((r) => setTimeout(r, 2000));

    await page.pdf({
      path: outFile,
      format: "A4",
      printBackground: true,
      margin: { top: "10mm", bottom: "10mm", left: "10mm", right: "10mm" },
      preferCSSPageSize: false,
    });

    return { dot, ok: true, file: outFile };
  } catch (err) {
    return { dot, ok: false, error: err.message };
  } finally {
    await browser.close();
  }
}

// Run tasks with a fixed concurrency pool
async function runPool(tasks, concurrency) {
  const results = [];
  const queue = [...tasks];
  const workers = Array.from({ length: concurrency }, async () => {
    while (queue.length) {
      const task = queue.shift();
      results.push(await task());
    }
  });
  await Promise.all(workers);
  return results;
}

// ── Entry point ──────────────────────────────────────────────────────────────
const args = process.argv.slice(2);
const isBatch = args[0] === "--batch";

let dots, accidentDate, outputDir;

if (isBatch) {
  dots = (args[1] || "").split(",").map(d => d.trim()).filter(Boolean);
  accidentDate = args[2] || null;
  outputDir = args[3] || DEFAULT_OUTPUT_DIR;
} else {
  const [dot, date, dir] = args;
  dots = [dot];
  accidentDate = date || null;
  outputDir = dir || DEFAULT_OUTPUT_DIR;
}

if (!dots.length || dots.some(d => !/^\d+$/.test(d))) {
  console.error("Usage:");
  console.error("  node generate_pdf.js <DOT> [MM/DD/YYYY] [FOLDER]");
  console.error("  node generate_pdf.js --batch <DOT1,DOT2,...> [MM/DD/YYYY] [FOLDER]");
  process.exit(1);
}

const outDir = resolve(outputDir);
if (!existsSync(outDir)) {
  mkdirSync(outDir, { recursive: true });
  console.log(`Created: ${outDir}`);
}

console.log(`Checking portal ...`);
if (!(await isPortalRunning())) {
  console.error("ERROR: carrier-portal not running. Start with: cd ../carrier-portal && npm run dev");
  process.exit(1);
}

const authCookie = getAuthCookieValue();
const total = dots.length;
console.log(`Generating ${total} PDF(s) with concurrency=${Math.min(CONCURRENCY, total)} ...`);
if (accidentDate) console.log(`Accident date: ${accidentDate}`);
console.log();

let done = 0;
const tasks = dots.map(dot => async () => {
  const result = await generatePdf(dot, accidentDate, outDir, authCookie);
  done++;
  if (result.ok) {
    console.log(`[${done}/${total}] ✓ DOT ${dot} → ${result.file.split(/[/\\]/).pop()}`);
  } else {
    console.error(`[${done}/${total}] ✗ DOT ${dot} FAILED: ${result.error}`);
  }
  return result;
});

const results = await runPool(tasks, CONCURRENCY);
const passed = results.filter(r => r.ok).length;
const failed = results.filter(r => !r.ok).length;

console.log();
console.log(`Done: ${passed} saved, ${failed} failed.`);
if (failed) process.exit(1);
