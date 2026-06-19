from pathlib import Path
import tempfile
from ssr.config import Settings
from ssr.permissions import PermissionManager, PermissionResult

def test_permissions_lifecycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        settings = Settings(
            home=tmp_path / "home",
            project_dir=tmp_path / "project",
        )
        settings.ensure_dirs()
        
        pm = PermissionManager(settings)
        
        # 1. Test default safe commands allowed
        assert pm.check_permission("ls") == PermissionResult.ALLOWED
        assert pm.check_permission("git status") == PermissionResult.ALLOWED
        assert pm.check_permission("git diff file.py") == PermissionResult.ALLOWED
        
        # 2. Test unknown command needs approval
        assert pm.check_permission("rm -rf /") == PermissionResult.NEEDS_APPROVAL
        assert pm.check_permission("curl google.com") == PermissionResult.NEEDS_APPROVAL
        
        # 3. Test add_always_allow persists and works
        pm.add_always_allow("curl *")
        assert pm.check_permission("curl google.com") == PermissionResult.ALLOWED
        
        # Reload and check persistence
        pm2 = PermissionManager(settings)
        assert pm2.check_permission("curl google.com") == PermissionResult.ALLOWED
        
        # 4. Test turbo_mode bypasses all
        pm2.turbo_mode = True
        assert pm2.check_permission("rm -rf /") == PermissionResult.ALLOWED
        
        # 5. Test glob pattern matching
        pm2.turbo_mode = False
        pm2.add_always_allow("rm *.txt")
        assert pm2.check_permission("rm file.txt") == PermissionResult.ALLOWED
        assert pm2.check_permission("rm file.log") == PermissionResult.NEEDS_APPROVAL
