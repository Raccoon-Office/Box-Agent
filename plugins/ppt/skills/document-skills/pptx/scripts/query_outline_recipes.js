#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");

const RECIPE_PATH = path.resolve(__dirname, "..", "references", "outline-recipes.json");

function parseArgs(argv) {
  const opts = { text: "", id: "", list: false };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    const value = argv[index + 1];
    if (arg === "--text" && value !== undefined) {
      opts.text = value;
      index += 1;
    } else if (arg === "--id" && value) {
      opts.id = value.trim();
      index += 1;
    } else if (arg === "--list") {
      opts.list = true;
    } else if (arg === "--help" || arg === "-h") {
      console.log("Usage: query_outline_recipes.js [--list] [--id RECIPE_ID] [--text SCENARIO]");
      process.exit(0);
    } else {
      throw new Error(`Unknown argument: ${arg}`);
    }
  }
  return opts;
}

function normalized(value) {
  return String(value || "").normalize("NFKC").toLocaleLowerCase().replace(/\s+/g, " ").trim();
}

function loadRecipes() {
  const payload = JSON.parse(fs.readFileSync(RECIPE_PATH, "utf8"));
  if (payload.schema_version !== 1 || !Array.isArray(payload.recipes)) {
    throw new Error("outline-recipes.json must declare schema_version 1 and recipes[]");
  }
  payload.recipes.forEach((recipe, index) => {
    if (!recipe || typeof recipe.id !== "string" || !Array.isArray(recipe.signals) || !Array.isArray(recipe.beats)) {
      throw new Error(`recipes.${index}: expected id, signals[], and beats[]`);
    }
  });
  return payload.recipes;
}

function compact(recipe) {
  return {
    id: recipe.id,
    name: recipe.name,
    summary: recipe.summary,
    signals: recipe.signals,
    beat_count: recipe.beats.length,
  };
}

function scoreRecipe(recipe, value) {
  const source = normalized(value);
  if (!source) return { score: 0, matched_signals: [] };
  const matchedSignals = recipe.signals.filter(signal => source.includes(normalized(signal)));
  return {
    score: matchedSignals.reduce((score, signal) => score + Math.max(1, normalized(signal).length), 0),
    matched_signals: matchedSignals,
  };
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const recipes = loadRecipes();
  if (opts.list) {
    console.log(JSON.stringify({ count: recipes.length, recipes: recipes.map(compact) }));
    return;
  }
  if (opts.id) {
    const recipe = recipes.find(item => item.id === opts.id);
    if (!recipe) throw new Error(`Unknown outline recipe: ${opts.id}`);
    console.log(JSON.stringify({ matched: true, source: "explicit", recipe }));
    return;
  }
  if (!opts.text) throw new Error("Use --list, --id RECIPE_ID, or --text SCENARIO");
  const ranked = recipes
    .map((recipe, index) => ({ recipe, index, ...scoreRecipe(recipe, opts.text) }))
    .sort((left, right) => right.score - left.score || left.index - right.index);
  const selected = ranked[0];
  if (!selected || selected.score === 0) {
    console.log(JSON.stringify({ matched: false, recipe: null }));
    return;
  }
  console.log(JSON.stringify({
    matched: true,
    source: "signal_match",
    score: selected.score,
    matched_signals: selected.matched_signals,
    recipe: selected.recipe,
  }));
}

try {
  main();
} catch (error) {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
}
