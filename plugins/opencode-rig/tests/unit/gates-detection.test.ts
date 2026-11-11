import { describe, expect, it } from "vitest"
import { detectGates } from "../../src/gates.ts"
import { Project } from "../helpers/harness.ts"

describe("gate configuration discovery", () => {
  it("retains pytest detection for valid TOML table whitespace", () => {
    const project = new Project()
    project.write("pyproject.toml", '[tool.pytest.ini_options ]\naddopts = "-q"\n')
    expect(detectGates(project.root, []).map((gate) => gate.command)).toEqual(["pytest"])
  })

  it.each([
    ["pytest.ini", "[pytest]\ntestpaths = tests\n"],
    ["setup.cfg", "[tool:pytest]\ntestpaths = tests\n"],
    ["tox.ini", "[pytest]\ntestpaths = tests\n"],
  ])("detects standalone %s", (file, text) => {
    const project = new Project()
    project.write(file, text)
    expect(detectGates(project.root, [])).toEqual([
      { id: "test", command: "pytest", source: "pytest configuration", needsFiles: false },
    ])
  })

  it.each(["setup.cfg", "tox.ini"])("ignores %s without a pytest section", (file) => {
    const project = new Project()
    project.write(file, "[metadata]\nname = demo\n")
    expect(detectGates(project.root, [])).toEqual([])
  })

  it("detects every standard target in a Makefile rule", () => {
    const project = new Project()
    project.write("Makefile", "lint format typecheck test: shared\n\t@echo checked\n")
    expect(detectGates(project.root, []).map((gate) => gate.command)).toEqual([
      "make lint",
      "make format",
      "make typecheck",
      "make test",
    ])
  })

  it("retains configured-source precedence", () => {
    const project = new Project()
    project.write("Makefile", "test lint:\n\t@echo checked\n")
    project.write("pytest.ini", "[pytest]\n")
    expect(detectGates(project.root, ["custom check"])).toEqual([
      { id: "configured-1", command: "custom check", source: "options.gates", needsFiles: false },
    ])
  })

  it.each([
    "define HELP\nlint test: example help\nendef\nall:\n\t@echo built\n",
    "override define HELP\nlint test: help\ndefine INNER\nformat: help\nendef\nendef\nall:\n",
  ])("ignores target-shaped text in Makefile definitions", (text) => {
    const project = new Project()
    project.write("Makefile", text)
    expect(detectGates(project.root, [])).toEqual([])
  })

  it("detects a real target following a Makefile definition", () => {
    const project = new Project()
    project.write("Makefile", "define HELP\nlint: help\nendef\ntest:\n\t@echo tested\n")
    expect(detectGates(project.root, []).map((gate) => gate.command)).toEqual(["make test"])
  })
})
