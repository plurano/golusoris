// SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
//
// SPDX-License-Identifier: EUPL-1.2

package twotier

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"go/types"
	"os"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

// TestSetL1IfCurrentCallsAreBounded catches test calls that can stall under mutation.
func TestSetL1IfCurrentCallsAreBounded(t *testing.T) {
	t.Parallel()
	for _, source := range readBoundedWaitTestSources(t) {
		positions, err := unboundedFillCalls(source.name, source.data)
		require.NoError(t, err)
		for _, position := range positions {
			t.Errorf("cache/twotier: %s: setL1IfCurrent call must be inside a startL2LockOperation worker", position)
		}
	}
}

type boundedWaitTestSource struct {
	name string
	data []byte
}

// readBoundedWaitTestSources includes test files behind build tags in the source checks.
func readBoundedWaitTestSources(t *testing.T) []boundedWaitTestSource {
	t.Helper()
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatalf("cache/twotier: list test sources: %v", err)
	}
	sources := make([]boundedWaitTestSource, 0, len(entries))
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, "_test.go") {
			continue
		}
		data, readErr := os.ReadFile(name)
		if readErr != nil {
			t.Fatalf("cache/twotier: read test source %s: %v", name, readErr)
		}
		sources = append(sources, boundedWaitTestSource{name: name, data: data})
	}
	return sources
}

// TestL2LockTestsArmWatchdog catches lock regression tests without a synchronous-call deadline.
func TestL2LockTestsArmWatchdog(t *testing.T) {
	t.Parallel()
	findings, err := missingL2LockWatchdogs(readBoundedWaitTestSources(t))
	require.NoError(t, err)
	for _, finding := range findings {
		t.Errorf("cache/twotier: %s: %s must start with armL2LockWatchdog after optional t.Parallel()", finding.position, finding.name)
	}
}

// TestL2StubHooksOnlyCallAllowedFunctions keeps test assertions off the stub's worker goroutine.
func TestL2StubHooksOnlyCallAllowedFunctions(t *testing.T) {
	t.Parallel()
	for _, source := range readBoundedWaitTestSources(t) {
		calls, err := disallowedL2HookCalls(source.name, source.data)
		require.NoError(t, err)
		for _, call := range calls {
			t.Errorf("cache/twotier: %s: L2 stub hook call to %s is outside the allow-list", call.position, call.callee)
		}
	}
}

type l2HookCall struct {
	position token.Position
	callee   string
}

// disallowedL2HookCalls checks every matching hook body, including its nested callbacks.
func disallowedL2HookCalls(filename string, src []byte) ([]l2HookCall, error) {
	files := token.NewFileSet()
	file, err := parser.ParseFile(files, filename, src, parser.SkipObjectResolution)
	if err != nil {
		return nil, fmt.Errorf("cache/twotier: parse test source %s: %w", filename, err)
	}
	var calls []l2HookCall
	ast.Inspect(file, func(node ast.Node) bool {
		hook, ok := node.(*ast.FuncLit)
		if !ok || !isL2StubHookType(hook.Type) {
			return true
		}
		ast.Inspect(hook.Body, func(child ast.Node) bool {
			if call, called := child.(*ast.CallExpr); called {
				callee := types.ExprString(ast.Unparen(call.Fun))
				if !isAllowedL2HookCall(callee) {
					calls = append(calls, l2HookCall{position: files.PositionFor(call.Pos(), false), callee: callee})
				}
			}
			return true
		})
		return false
	})
	return calls, nil
}

// isL2StubHookType matches the stub signature with grouped, separate, or unnamed parameters.
func isL2StubHookType(signature *ast.FuncType) bool {
	var parameters [3]ast.Expr
	if signature.Params.NumFields() != len(parameters) || signature.Results.NumFields() != 0 {
		return false
	}
	index := 0
	for _, field := range signature.Params.List {
		for range max(1, len(field.Names)) {
			parameters[index] = ast.Unparen(field.Type)
			index++
		}
	}
	value, isArrayType := parameters[2].(*ast.ArrayType)
	return types.ExprString(parameters[0]) == "string" && types.ExprString(parameters[1]) == "string" &&
		isArrayType && value.Len == nil && types.ExprString(ast.Unparen(value.Elt)) == "byte"
}

// isAllowedL2HookCall limits the hook to the calls the current stubs need.
func isAllowedL2HookCall(callee string) bool {
	switch callee {
	case "tryStaleL1Install", "panicOnce.Do", "panic", "string", "signal":
		return true
	default:
		return false
	}
}

type l2LockWatchdogFinding struct {
	position token.Position
	name     string
}

// missingL2LockWatchdogs reports tests that reach a lock worker without arming a watchdog.
func missingL2LockWatchdogs(sources []boundedWaitTestSource) ([]l2LockWatchdogFinding, error) {
	files := token.NewFileSet()
	decls, err := l2LockTestDecls(files, sources)
	if err != nil {
		return nil, err
	}
	helpers := l2LockHelpers(decls)
	var findings []l2LockWatchdogFinding
	for _, decl := range decls {
		if strings.HasPrefix(decl.Name.Name, "Test") && callsL2LockHelper(decl.Body, helpers) && !startsWithL2LockWatchdog(decl.Body.List) {
			findings = append(findings, l2LockWatchdogFinding{position: files.PositionFor(decl.Pos(), false), name: decl.Name.Name})
		}
	}
	return findings, nil
}

// l2LockTestDecls gathers function bodies before resolving calls across test files.
func l2LockTestDecls(files *token.FileSet, sources []boundedWaitTestSource) ([]*ast.FuncDecl, error) {
	var decls []*ast.FuncDecl
	for _, source := range sources {
		file, err := parser.ParseFile(files, source.name, source.data, parser.SkipObjectResolution)
		if err != nil {
			return nil, fmt.Errorf("cache/twotier: parse test source %s: %w", source.name, err)
		}
		for _, decl := range file.Decls {
			if function, ok := decl.(*ast.FuncDecl); ok && function.Body != nil {
				decls = append(decls, function)
			}
		}
	}
	return decls, nil
}

// l2LockHelpers propagates worker reachability until a pass adds no helpers.
func l2LockHelpers(decls []*ast.FuncDecl) map[string]bool {
	helpers := map[string]bool{"startL2LockOperation": true}
	for range decls {
		added := false
		for _, decl := range decls {
			name := decl.Name.Name
			if helpers[name] || strings.HasPrefix(name, "Test") || !callsL2LockHelper(decl.Body, helpers) {
				continue
			}
			helpers[name] = true
			added = true
		}
		if !added {
			break
		}
	}
	return helpers
}

// callsL2LockHelper checks calls inside a function body, including its subtest literals.
func callsL2LockHelper(body *ast.BlockStmt, helpers map[string]bool) bool {
	found := false
	ast.Inspect(body, func(node ast.Node) bool {
		if call, ok := node.(*ast.CallExpr); ok {
			switch callee := ast.Unparen(call.Fun).(type) {
			case *ast.Ident:
				found = found || helpers[callee.Name]
			case *ast.SelectorExpr:
				found = found || helpers[callee.Sel.Name]
			}
		}
		return !found
	})
	return found
}

// startsWithL2LockWatchdog accepts a watchdog call before setup starts.
func startsWithL2LockWatchdog(statements []ast.Stmt) bool {
	if len(statements) > 0 && isL2LockParallelCall(statements[0]) {
		statements = statements[1:]
	}
	if len(statements) == 0 {
		return false
	}
	callee := statementCallee(statements[0])
	name, ok := callee.(*ast.Ident)
	return ok && name.Name == "armL2LockWatchdog"
}

// isL2LockParallelCall recognizes the optional leading t.Parallel statement.
func isL2LockParallelCall(statement ast.Stmt) bool {
	selector, ok := statementCallee(statement).(*ast.SelectorExpr)
	if !ok || selector.Sel.Name != "Parallel" {
		return false
	}
	receiver, named := ast.Unparen(selector.X).(*ast.Ident)
	return named && receiver.Name == "t"
}

// statementCallee extracts the callee of a standalone call statement.
func statementCallee(statement ast.Stmt) ast.Expr {
	expression, ok := statement.(*ast.ExprStmt)
	if !ok {
		return nil
	}
	call, called := ast.Unparen(expression.X).(*ast.CallExpr)
	if !called {
		return nil
	}
	return ast.Unparen(call.Fun)
}

// unboundedFillCalls finds fill calls outside bounded worker literals.
func unboundedFillCalls(filename string, src []byte) ([]token.Position, error) {
	files := token.NewFileSet()
	file, err := parser.ParseFile(files, filename, src, parser.SkipObjectResolution)
	if err != nil {
		return nil, fmt.Errorf("cache/twotier: parse test source %s: %w", filename, err)
	}
	var positions []token.Position
	ast.PreorderStack(file, nil, func(node ast.Node, ancestors []ast.Node) bool {
		call, ok := node.(*ast.CallExpr)
		if !ok {
			return true
		}
		selector, selected := ast.Unparen(call.Fun).(*ast.SelectorExpr)
		if selected && selector.Sel.Name == "setL1IfCurrent" && !insideL2LockWorker(ancestors) {
			positions = append(positions, files.PositionFor(call.Pos(), false))
		}
		return true
	})
	return positions, nil
}

// insideL2LockWorker requires a run literal in the nearest enclosing startL2LockOperation call.
func insideL2LockWorker(ancestors []ast.Node) bool {
	const runArgIndex = 2
	for index, ancestor := range slices.Backward(ancestors) {
		call, ok := ancestor.(*ast.CallExpr)
		if !ok {
			continue
		}
		callee, named := ast.Unparen(call.Fun).(*ast.Ident)
		if !named || callee.Name != "startL2LockOperation" {
			continue
		}
		if len(call.Args) <= runArgIndex || index+1 == len(ancestors) {
			return false
		}
		runArg := call.Args[runArgIndex]
		_, literal := ast.Unparen(runArg).(*ast.FuncLit)
		return literal && ancestors[index+1] == runArg
	}
	return false
}

// TestSetL1IfCurrentCallsAreBoundedFixtures accepts worker calls and flags direct hook and release calls.
func TestSetL1IfCurrentCallsAreBoundedFixtures(t *testing.T) {
	t.Parallel()
	const planted = `package twotier
func test() {
	stub.beforeWrite = func() {
		view.setL1IfCurrent("n:a", 1, epoch)
	}
}
`
	const clean = `package twotier
func test() {
	stub.beforeWrite = func() {
		startL2LockOperation(t, nil, func() error {
			view.setL1IfCurrent("n:a", 1, epoch)
			return nil
		})
	}
}
`
	const release = `package twotier
func test() {
	startL2LockOperation(t, func() {
		view.setL1IfCurrent("n:a", 1, epoch)
	}, run)
}
`
	const nestedRelease = `package twotier
func test() {
	startL2LockOperation(t, nil, func() error {
		startL2LockOperation(t, func() {
			view.setL1IfCurrent("n:a", 1, epoch)
		}, run)
		return nil
	})
}
`
	positions, err := unboundedFillCalls("planted_test.go", []byte(planted))
	require.NoError(t, err)
	require.Len(t, positions, 1)
	require.Equal(t, "planted_test.go", positions[0].Filename)
	require.Equal(t, 4, positions[0].Line)

	cleanPositions, scanErr := unboundedFillCalls("clean_test.go", []byte(clean))
	require.NoError(t, scanErr)
	require.Empty(t, cleanPositions)

	releasePositions, releaseErr := unboundedFillCalls("release_test.go", []byte(release))
	require.NoError(t, releaseErr)
	require.Len(t, releasePositions, 1)
	require.Equal(t, "release_test.go", releasePositions[0].Filename)
	require.Equal(t, 4, releasePositions[0].Line)

	nestedPositions, nestedErr := unboundedFillCalls("nested_release_test.go", []byte(nestedRelease))
	require.NoError(t, nestedErr)
	require.Len(t, nestedPositions, 1)
	require.Equal(t, "nested_release_test.go", nestedPositions[0].Filename)
	require.Equal(t, 5, nestedPositions[0].Line)
}

// TestL2LockTestsArmWatchdogFixtures covers direct and transitive calls across test files.
func TestL2LockTestsArmWatchdogFixtures(t *testing.T) {
	t.Parallel()
	const planted = `package twotier
func TestPlanted(t *testing.T) {
	startL2LockOperation(t, nil, run)
}
`
	const helperPlanted = `package twotier
func TestHelperPlanted(t *testing.T) {
	helper(t)
}
func helper(t *testing.T) {
	workerHelper(t)
}
`
	const clean = `package twotier
func TestClean(t *testing.T) {
	t.Parallel()
	armL2LockWatchdog(t)
	startL2LockOperation(t, nil, run)
}
func workerHelper(t *testing.T) {
	startL2LockOperation(t, nil, run)
}
`
	findings, err := missingL2LockWatchdogs([]boundedWaitTestSource{
		{name: "planted_test.go", data: []byte(planted)},
		{name: "helper_planted_test.go", data: []byte(helperPlanted)},
		{name: "clean_test.go", data: []byte(clean)},
	})
	require.NoError(t, err)
	require.Len(t, findings, 2)
	require.Equal(t, "planted_test.go:2:1", findings[0].position.String())
	require.Equal(t, "TestPlanted", findings[0].name)
	require.Equal(t, "helper_planted_test.go:2:1", findings[1].position.String())
	require.Equal(t, "TestHelperPlanted", findings[1].name)
}

// TestL2StubHooksOnlyCallAllowedFunctionsFixtures plants direct and nested test failures.
func TestL2StubHooksOnlyCallAllowedFunctionsFixtures(t *testing.T) {
	t.Parallel()
	const source = `package twotier
func test() {
	stub.beforeWrite = func(op, key string, _ []byte) {
		waitL2LockSignal(t, done, "worker stalled")
	}
	stub.afterWrite = func(string, string, []byte) {
		panicOnce.Do(func() { t.Fatal("worker failed") })
	}
	hook := func(op string, key string, _ []byte) {
		signal()
		<-blocked
	}
}
`
	calls, err := disallowedL2HookCalls("fixture_test.go", []byte(source))
	require.NoError(t, err)
	require.Len(t, calls, 2)
	require.Equal(t, "fixture_test.go", calls[0].position.Filename)
	require.Equal(t, 4, calls[0].position.Line)
	require.Equal(t, "waitL2LockSignal", calls[0].callee)
	require.Equal(t, "fixture_test.go", calls[1].position.Filename)
	require.Equal(t, 7, calls[1].position.Line)
	require.Equal(t, "t.Fatal", calls[1].callee)
}

// TestL2LockWatchdogExpires verifies that expiry names the blocked test and includes its stacks.
func TestL2LockWatchdogExpires(t *testing.T) {
	t.Parallel()
	t.Run("blocked", func(subtest *testing.T) {
		subtest.Parallel()
		expired := make(chan string, 1)
		armL2LockWatchdogFor(subtest, 10*time.Millisecond, func(message string) { expired <- message })
		select {
		case message := <-expired:
			require.Contains(subtest, message, subtest.Name())
			require.Contains(subtest, message, "still running after")
			require.Contains(subtest, message, "goroutine ")
		case <-time.After(l2LockTimeout):
			subtest.Fatal("cache/twotier: watchdog did not expire")
		}
	})
}

// TestL2LockWatchdogStopsAtCleanup proves a completed subtest cannot trigger expiry.
//
//nolint:tparallel // The parent must observe the subtest after its cleanup finishes.
func TestL2LockWatchdogStopsAtCleanup(t *testing.T) {
	t.Parallel()
	expired := make(chan string, 1)
	t.Run("completed", func(subtest *testing.T) {
		armL2LockWatchdogFor(subtest, time.Second, func(message string) { expired <- message })
	})
	select {
	case message := <-expired:
		t.Fatalf("cache/twotier: watchdog expired after cleanup: %s", message)
	case <-time.After(1500 * time.Millisecond):
	}
}
