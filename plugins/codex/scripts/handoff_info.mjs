#!/usr/bin/env node
import { main } from "./jieli_node.mjs";

process.exit(await main("handoff-info"));
