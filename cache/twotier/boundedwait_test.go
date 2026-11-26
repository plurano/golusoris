// SPDX-FileCopyrightText: 2026 lusoris <lusoris@pm.me>
//
// SPDX-License-Identifier: EUPL-1.2

package twotier

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

// TestSetL1IfCurrentCallsAreBounded catches test calls that can stall under mutation.
func TestSetL1IfCurrentCallsAreBounded(t *testing.T) {
	t.Parallel()
	entries, err := os.ReadDir(".")
	if err != nil {
		t.Fatalf("cache/twotier: list test sources: %v", err)
	}
	for _, entry := range entries {
		name := entry.Name()
		if entry.IsDir() || !strings.HasSuffix(name, "_test.go") {
			continue
		}
		checkBoundedFillCalls(t, name)
	}
}

// checkBoundedFillCalls reports every unbounded call in a test source.
func checkBoundedFillCalls(t *testing.T, name string) {
	t.Helper()
	src, err := os.ReadFile(name)
	if err != nil {
		t.Fatalf("cache/twotier: read test source %s: %v", name, err)
	}
	positions, scanErr := unboundedFillCalls(name, src)
	require.NoError(t, scanErr)
	for _, position := range positions {
		t.Errorf("cache/twotier: %s: setL1IfCurrent call must be inside a startL2LockOperation worker", position)
	}
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

// insideL2LockWorker matches a function literal passed directly to startL2LockOperation.
func insideL2LockWorker(ancestors []ast.Node) bool {
	for index := range len(ancestors) - 1 {
		call, ok := ancestors[index].(*ast.CallExpr)
		if !ok {
			continue
		}
		callee, named := ast.Unparen(call.Fun).(*ast.Ident)
		if !named || callee.Name != "startL2LockOperation" {
			continue
		}
		arg, expression := ancestors[index+1].(ast.Expr)
		if !expression {
			continue
		}
		if _, literal := ast.Unparen(arg).(*ast.FuncLit); literal {
			return true
		}
	}
	return false
}

// TestSetL1IfCurrentCallsAreBoundedFixtures distinguishes a direct hook call from a worker.
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
	positions, err := unboundedFillCalls("planted_test.go", []byte(planted))
	require.NoError(t, err)
	require.Len(t, positions, 1)
	require.Equal(t, "planted_test.go", positions[0].Filename)
	require.Equal(t, 4, positions[0].Line)

	cleanPositions, scanErr := unboundedFillCalls("clean_test.go", []byte(clean))
	require.NoError(t, scanErr)
	require.Empty(t, cleanPositions)
}
