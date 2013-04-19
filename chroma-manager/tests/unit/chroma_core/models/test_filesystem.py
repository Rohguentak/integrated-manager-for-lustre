from chroma_core.models import ManagedMgs, ManagedFilesystem, ManagedMdt, ManagedOst
from django.test import TestCase
from tests.unit.chroma_core.helper import synthetic_host, synthetic_volume_full, load_default_profile


class TestNidStrings(TestCase):
    def setUp(self):
        # If the test that just ran imported storage_plugin_manager, it will
        # have instantiated its singleton, and created some DB records.
        # Django TestCase rolls back the database, so make sure that we
        # also roll back (reset) this singleton.
        import chroma_core.lib.storage_plugin.manager
        chroma_core.lib.storage_plugin.manager.storage_plugin_manager = chroma_core.lib.storage_plugin.manager.StoragePluginManager()

        load_default_profile()

    def _host_with_nids(self, address):
        host_nids = {
            'primary-mgs': ['1.2.3.4@tcp'],
            'failover-mgs': ['1.2.3.5@tcp'],
            'primary-mgs-twonid': ['1.2.3.4@tcp', '4.3.2.1@tcp1'],
            'failover-mgs-twonid': ['1.2.3.5@tcp', '4.3.2.2@tcp1'],
            'othernode': ['1.2.3.6@tcp', '4.3.2.3@tcp1']
        }
        return synthetic_host(address, host_nids[address])

    def test_one_nid_no_failover(self):
        mgs0 = self._host_with_nids('primary-mgs')
        other = self._host_with_nids('othernode')
        mgt, _ = ManagedMgs.create_for_volume(synthetic_volume_full(mgs0).id, name = "MGS")
        fs = ManagedFilesystem.objects.create(mgs = mgt, name = "testfs")
        ManagedMdt.create_for_volume(synthetic_volume_full(other).id, filesystem = fs)
        ManagedOst.create_for_volume(synthetic_volume_full(other).id, filesystem = fs)

        self.assertEqual(mgt.nids(), ((u'1.2.3.4@tcp0',),))
        self.assertEqual(fs.mgs_spec(), u'1.2.3.4@tcp0')

    def test_one_nid_with_failover(self):
        mgs0 = self._host_with_nids('primary-mgs')
        mgs1 = self._host_with_nids('failover-mgs')
        other = self._host_with_nids('othernode')
        mgt, _ = ManagedMgs.create_for_volume(synthetic_volume_full(mgs0, mgs1).id, name = "MGS")
        fs = ManagedFilesystem.objects.create(mgs = mgt, name = "testfs")
        ManagedMdt.create_for_volume(synthetic_volume_full(other).id, filesystem = fs)
        ManagedOst.create_for_volume(synthetic_volume_full(other).id, filesystem = fs)

        self.assertEqual(mgt.nids(), ((u'1.2.3.4@tcp0',), (u'1.2.3.5@tcp0',)))
        self.assertEqual(fs.mgs_spec(), u'1.2.3.4@tcp0:1.2.3.5@tcp0')

    def test_two_nids_no_failover(self):
        mgs0 = self._host_with_nids('primary-mgs-twonid')
        other = self._host_with_nids('othernode')
        mgt, _ = ManagedMgs.create_for_volume(synthetic_volume_full(mgs0).id, name = "MGS")
        fs = ManagedFilesystem.objects.create(mgs = mgt, name = "testfs")
        ManagedMdt.create_for_volume(synthetic_volume_full(other).id, filesystem = fs)
        ManagedOst.create_for_volume(synthetic_volume_full(other).id, filesystem = fs)

        self.assertEqual(mgt.nids(), ((u'1.2.3.4@tcp0', u'4.3.2.1@tcp1'),))
        self.assertEqual(fs.mgs_spec(), u'1.2.3.4@tcp0,4.3.2.1@tcp1')

    def test_two_nids_with_failover(self):
        mgs0 = self._host_with_nids('primary-mgs-twonid')
        mgs1 = self._host_with_nids('failover-mgs-twonid')
        other = self._host_with_nids('othernode')
        mgt, _ = ManagedMgs.create_for_volume(synthetic_volume_full(mgs0, mgs1).id, name = "MGS")
        fs = ManagedFilesystem.objects.create(mgs = mgt, name = "testfs")
        ManagedMdt.create_for_volume(synthetic_volume_full(other).id, filesystem = fs)
        ManagedOst.create_for_volume(synthetic_volume_full(other).id, filesystem = fs)

        self.assertEqual(mgt.nids(), ((u'1.2.3.4@tcp0', u'4.3.2.1@tcp1'), (u'1.2.3.5@tcp0', u'4.3.2.2@tcp1')))
        self.assertEqual(fs.mgs_spec(), u'1.2.3.4@tcp0,4.3.2.1@tcp1:1.2.3.5@tcp0,4.3.2.2@tcp1')
